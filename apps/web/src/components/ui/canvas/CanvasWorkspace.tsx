"use client";

import dynamic from "next/dynamic";
import { X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchVideoOptions } from "@/lib/video/requestLifecycle";
import { createCanvas } from "@/lib/api/canvases";
import {
  canvasVideoCapabilityError,
  validateCanvasNodeExecution,
} from "@/lib/canvas/graph";
import { blurActiveCanvasEditor } from "@/lib/canvas/interaction";
import { type PersistedCanvasSaveBatch } from "@/lib/canvas/persistence";
import { isCanvasVideoNodeType } from "@/lib/canvas/registry";
import type { CanvasDocument, CanvasGraph } from "@/lib/canvas/types";
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
import {
  CanvasInspector,
  type CanvasInspectorProps,
} from "./CanvasInspector";
import { CanvasNodePalette } from "./CanvasNodePalette";
import { CanvasSelectionToolbar } from "./CanvasSelectionToolbar";
import { CanvasShortcutsDialog } from "./CanvasShortcutsDialog";
import {
  CanvasStoreProvider,
  useCanvasStore,
  useCanvasStoreApi,
} from "./CanvasStoreProvider";
import { CanvasTopBar } from "./CanvasTopBar";
import type { CanvasViewportApi } from "./CanvasViewport";
import { CanvasMobileToolbar } from "./mobile/CanvasMobileToolbar";
import {
  useCanvasFullscreen,
  useCanvasKeyboardShortcuts,
} from "./CanvasWorkspaceInteractions";
import {
  browserClientId,
  randomId,
  useCanvasAutosave,
  useCanvasClientLease,
  useCanvasDraftPersistence,
  useCanvasTabCoordination,
  useRemoteDocumentSync,
} from "./CanvasWorkspacePersistence";
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
          description={
            query.error instanceof Error ? query.error.message : "网络异常"
          }
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
  const isMobile = useMediaQuery("(max-width: 767px)") !== false;
  const isCompact = useMediaQuery("(max-width: 1199px)") !== false;
  const store = useCanvasStoreApi();
  const graph = useCanvasStore((state) => state.graph);
  const revision = useCanvasStore((state) => state.revision);
  const saveState = useCanvasStore((state) => state.saveState);
  const saveMessage = useCanvasStore((state) => state.saveMessage);
  const pendingCount = useCanvasStore(
    (state) => state.pendingOperations.length,
  );
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
  const hasInspectorSelection =
    selectedNodeIds.length > 0 || Boolean(selectedEdgeId);
  const [title, setTitle] = useState(document.title);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [viewportApi, setViewportApi] = useState<CanvasViewportApi | null>(
    null,
  );
  const [durabilityWarning, setDurabilityWarning] = useState<string | null>(
    null,
  );
  const { fullscreen, toggleFullscreen, exitFullscreen } =
    useCanvasFullscreen();
  const [tabId] = useState(randomId);
  const [clientId] = useState(() => browserClientId(canvasId, tabId));
  const recoveredSaveBatchRef = useRef<PersistedCanvasSaveBatch | null>(null);
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
        const node = store
          .getState()
          .graph.nodes.find((candidate) => candidate.id === nodeId);
        if (node && isCanvasVideoNodeType(node.type)) {
          let options;
          const controller = new AbortController();
          const timeout = window.setTimeout(() => controller.abort(), 10_000);
          try {
            options = await fetchVideoOptions(controller.signal);
          } catch (error) {
            toast.error(
              controller.signal.aborted
                ? "视频能力加载超时，请重试"
                : error instanceof Error
                  ? error.message
                  : "视频能力加载失败",
            );
            return;
          } finally {
            window.clearTimeout(timeout);
          }
          const capabilityError = canvasVideoCapabilityError(
            node,
            options,
            store.getState().graph,
          );
          if (capabilityError) {
            toast.error(capabilityError);
            return;
          }
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
      titleMutationQueueRef.current = titleMutationQueueRef.current.then(
        saveTitle,
        saveTitle,
      );
    },
    [patchCanvas],
  );
  const closeMobileInspector = useCallback(() => {
    setInspectorOpen(false);
    store.getState().selectNode(null);
    store.getState().selectEdge(null);
  }, [store]);

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
              toast.error(
                error instanceof Error ? error.message : "副本创建失败",
              );
            }
          }}
        />
      ) : null}
      {durabilityWarning && pendingCount > 0 ? (
        <DurabilityBanner message={durabilityWarning} />
      ) : null}

      <div
        className={cn(
          "grid min-h-0 flex-1",
          hasInspectorSelection
            ? "min-[1200px]:grid-cols-[248px_minmax(0,1fr)_352px]"
            : "min-[1200px]:grid-cols-[248px_minmax(0,1fr)]",
        )}
      >
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
              className="absolute bottom-3 left-1/2 z-[var(--z-tabbar)] -translate-x-1/2 max-[767px]:hidden"
            />
          </div>
          <div className="hidden max-[767px]:contents">
            <CanvasMobileToolbar
              onAdd={() => setPaletteOpen(true)}
              onFitView={() => viewportApi?.fitView()}
              onOpenCommandMenu={() => tools.openCommandMenu(null)}
            />
          </div>
        </main>

        <CanvasInspectorSurfaces
          isMobile={isMobile}
          isCompact={isCompact}
          open={inspectorOpen}
          hasSelection={hasInspectorSelection}
          onClose={() => setInspectorOpen(false)}
          onCloseMobile={closeMobileInspector}
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

      <BottomSheet
        open={isMobile && paletteOpen}
        onClose={() => setPaletteOpen(false)}
        ariaLabel="添加节点"
        snapPoints={["82%"]}
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

function CanvasInspectorSurfaces({
  isMobile,
  isCompact,
  open,
  hasSelection,
  onClose,
  onCloseMobile,
  ...inspectorProps
}: CanvasInspectorProps & {
  isMobile: boolean;
  isCompact: boolean;
  open: boolean;
  hasSelection: boolean;
  onClose: () => void;
  onCloseMobile: () => void;
}) {
  const showTabletInspector = !isMobile && isCompact && open;

  return (
    <>
      {hasSelection ? (
        <aside className="hidden min-h-0 border-l border-[var(--border)] bg-[var(--bg-1)] min-[1200px]:block">
          <CanvasInspector {...inspectorProps} />
        </aside>
      ) : null}

      {showTabletInspector ? (
        <div className="pointer-events-none absolute inset-0 z-[var(--z-popover)] flex justify-end p-3">
          <aside className="pointer-events-auto relative flex h-full w-[min(352px,calc(100vw-24px))] min-h-0 flex-col border border-[var(--border)] bg-[var(--bg-1)] shadow-[var(--shadow-2)]">
            <IconButton
              aria-label="关闭检查器"
              size="lg"
              onClick={onClose}
              className="absolute right-2 top-2 z-[var(--z-tabbar)]"
            >
              <X className="h-4 w-4" />
            </IconButton>
            <CanvasInspector {...inspectorProps} />
          </aside>
        </div>
      ) : null}

      {isMobile ? (
        <BottomSheet
          open={open}
          onClose={onCloseMobile}
          ariaLabel="节点检查器"
          snapPoints={["88%"]}
          className="mobile-dialog-sheet"
        >
          <div className="relative h-full min-h-0">
            <IconButton
              aria-label="关闭检查器"
              size="lg"
              onClick={onCloseMobile}
              className="absolute right-3 top-3 z-[var(--z-tabbar)]"
            >
              <X className="h-4 w-4" />
            </IconButton>
            <CanvasInspector {...inspectorProps} />
          </div>
        </BottomSheet>
      ) : null}
    </>
  );
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
