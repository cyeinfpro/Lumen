"use client";

import {
  ArrowDown,
  ArrowUp,
  Check,
  Image as ImageIcon,
  Loader2,
  Trash2,
  Video,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  fetchVideoOptions,
  uploadReferenceVideo,
} from "@/lib/video/requestLifecycle";
import {
  imageBinaryUrl,
  imageVariantUrl,
  uploadImage,
  videoPosterUrl,
} from "@/lib/apiClient";
import { cleanupStaleCanvasUpload } from "@/lib/canvas/staleUploadCleanup";
import { normalizeCanvasNodeTitle } from "@/lib/canvas/constants";
import type {
  CanvasDocument,
  CanvasEdgeDefinition,
  CanvasEdgeDetailsUpdate,
  CanvasNodeDefinition,
  CanvasOutput,
} from "@/lib/canvas/types";
import {
  CANVAS_NODE_SPECS,
  isCanvasExecutableNodeType,
} from "@/lib/canvas/registry";
import type { CanvasEditorStore } from "@/lib/canvas/store";
import { Button, toast } from "@/components/ui/primitives";
import {
  InspectorSection,
  InspectorShell,
  ReadOnlyRow,
  SelectField,
} from "./CanvasInspectorFields";
import type { SelectOption } from "./CanvasInspectorFields";
import { CanvasBatchInspector } from "./CanvasInspectorBatch";
import type { CanvasInspectorProps } from "./CanvasInspectorContracts";
import { CanvasInspectorExecutionHistory } from "./CanvasInspectorExecutionHistory";
import type { CanvasHistoryOutputProps } from "./CanvasInspectorExecutionHistory";
import {
  canvasNodePreset,
  incompatibleVideoConnectionCount,
  inspectorRunDisabledReason,
  inspectorVideoRunDisabledReason,
  queryErrorMessage,
} from "./CanvasInspectorModel";
import { CanvasInspectorNodePanel } from "./CanvasInspectorNodePanel";
import { CanvasOutputDownloadButton } from "./CanvasOutputDownloadButton";
import {
  useCanvasStore,
  useCanvasStoreApi,
} from "./CanvasStoreProvider";

export type {
  CanvasInspectorProps,
  CanvasSelectionAlignment,
  CanvasSelectionDistribution,
} from "./CanvasInspectorContracts";

interface PendingConfigChange {
  nodeId: string;
  changes: Record<string, unknown>;
  removedConnections: number;
}

type CanvasEdgeRole = NonNullable<CanvasEdgeDefinition["role"]>;

const DATA_TYPE_LABELS: Record<CanvasEdgeDefinition["data_type"], string> = {
  text: "文本",
  image: "图片",
  video: "视频",
  mask: "遮罩",
};

const EDGE_ROLE_OPTIONS: readonly SelectOption[] = [
  { value: "", label: "未指定" },
  { value: "reference", label: "通用参考" },
  { value: "subject", label: "主体" },
  { value: "product", label: "商品" },
  { value: "style", label: "风格" },
  { value: "edit_target", label: "编辑目标" },
  { value: "background", label: "背景" },
  { value: "other", label: "其他" },
];

export function CanvasInspector({
  document,
  onRunNode,
  runningNodeId,
  onDuplicateSelection,
  onAlignSelection,
  onDistributeSelection,
  onAutoLayoutSelection,
  onFitSelection,
}: CanvasInspectorProps) {
  const graph = useCanvasStore((state) => state.graph);
  const selectedNodeId = useCanvasStore((state) => state.selectedNodeId);
  const selectedNodeIds = useCanvasStore((state) => state.selectedNodeIds);
  const selectedEdgeId = useCanvasStore((state) => state.selectedEdgeId);
  const removeNodes = useCanvasStore((state) => state.removeNodes);
  const removeEdges = useCanvasStore((state) => state.removeEdges);
  const updateEdgeBinding = useCanvasStore(
    (state) => state.updateEdgeBinding,
  );
  const updateEdgeDetails = useCanvasStore(
    (state) => state.updateEdgeDetails,
  );

  const node = graph.nodes.find((item) => item.id === selectedNodeId) ?? null;
  const edge = graph.edges.find((item) => item.id === selectedEdgeId) ?? null;
  const selectedNodes = useMemo(() => {
    const selected = new Set(selectedNodeIds);
    return graph.nodes.filter((item) => selected.has(item.id));
  }, [graph.nodes, selectedNodeIds]);
  if (edge) {
    return (
      <CanvasEdgeInspector
        document={document}
        edge={edge}
        graph={graph}
        onRemove={(edgeId) => removeEdges([edgeId])}
        onUpdateBinding={updateEdgeBinding}
        onUpdateDetails={updateEdgeDetails}
      />
    );
  }

  if (selectedNodes.length > 1) {
    return (
      <CanvasBatchInspector
        nodes={selectedNodes}
        onDuplicateSelection={onDuplicateSelection}
        onAlignSelection={onAlignSelection}
        onDistributeSelection={onDistributeSelection}
        onAutoLayoutSelection={onAutoLayoutSelection}
        onFitSelection={onFitSelection}
        onDeleteSelection={() => removeNodes(selectedNodes.map((item) => item.id))}
      />
    );
  }

  if (!node) {
    return (
      <div className="grid h-full min-h-0 place-items-center px-6 text-center">
        <div>
          <p className="type-page-kicker">检查器</p>
          <h2 className="type-card-title mt-2">选择节点</h2>
          <p className="type-body-sm mt-2 max-w-[240px] text-[var(--fg-2)]">
            参数、输入绑定与历史输出会显示在这里。
          </p>
        </div>
      </div>
    );
  }

  return (
    <CanvasNodeInspector
      key={node.id}
      document={document}
      node={node}
      onRunNode={onRunNode}
      runningNodeId={runningNodeId}
    />
  );
}

function CanvasNodeInspector({
  document,
  node,
  onRunNode,
  runningNodeId,
}: {
  document: CanvasDocument;
  node: CanvasNodeDefinition;
  onRunNode: (nodeId: string) => void;
  runningNodeId?: string | null;
}) {
  const store = useCanvasStoreApi();
  const graph = useCanvasStore((state) => state.graph);
  const updateNodeConfig = useCanvasStore((state) => state.updateNodeConfig);
  const updateNodeTitle = useCanvasStore((state) => state.updateNodeTitle);
  const updateNodeAppearance = useCanvasStore(
    (state) => state.updateNodeAppearance,
  );
  const removeNodes = useCanvasStore((state) => state.removeNodes);
  const [pendingConfigChange, setPendingConfigChange] =
    useState<PendingConfigChange | null>(null);
  const assetUpload = useCanvasAssetUpload(store, node.id);
  const executions = useMemo(
    () =>
      document.recent_executions.filter(
        (execution) => execution.node_id === node.id,
      ),
    [document.recent_executions, node.id],
  );
  const videoOptionsQuery = useQuery({
    queryKey: ["video-options"],
    queryFn: ({ signal }) => fetchVideoOptions(signal),
    enabled: CANVAS_NODE_SPECS[node.type].family === "video",
    staleTime: 60_000,
  });
  const patch = (next: Record<string, unknown>) => {
    const nextConfig = { ...node.config, ...next };
    const removedConnections = incompatibleVideoConnectionCount(
      graph,
      node,
      nextConfig,
    );
    if (removedConnections > 0) {
      setPendingConfigChange({
        nodeId: node.id,
        changes: next,
        removedConnections,
      });
      return;
    }
    setPendingConfigChange(null);
    updateNodeConfig(node.id, nextConfig);
  };
  const canRun = isCanvasExecutableNodeType(node.type);
  const preset = canvasNodePreset(node);
  const visiblePendingChange =
    pendingConfigChange?.nodeId === node.id ? pendingConfigChange : null;
  const videoOptionsError = queryErrorMessage(
    videoOptionsQuery.isError,
    videoOptionsQuery.error,
    "视频能力加载失败",
  );
  const runDisabledReason =
    inspectorRunDisabledReason(graph, node) ??
    inspectorVideoRunDisabledReason(
      graph,
      node,
      videoOptionsQuery.data,
      videoOptionsQuery.isLoading,
      videoOptionsError,
    );

  return (
    <CanvasInspectorNodePanel
      node={node}
      eyebrow={preset?.label ?? CANVAS_NODE_SPECS[node.type].label}
      graph={graph}
      patch={patch}
      uploading={assetUpload.uploading}
      onUploadImage={assetUpload.uploadImage}
      onUploadVideo={assetUpload.uploadVideo}
      videoOptions={videoOptionsQuery.data}
      videoOptionsLoading={videoOptionsQuery.isLoading}
      videoOptionsError={videoOptionsError}
      videoOptionsRetrying={
        videoOptionsQuery.isFetching && !videoOptionsQuery.isLoading
      }
      onRetryVideoOptions={() => {
        void videoOptionsQuery.refetch();
      }}
      pendingConfigChange={visiblePendingChange}
      history={
        executions.length > 0 ? (
          <CanvasInspectorExecutionHistory
            executions={executions}
            document={document}
            selectedNodeId={node.id}
            OutputComponent={HistoryOutput}
          />
        ) : null
      }
      canRun={canRun}
      runDisabledReason={runDisabledReason}
      running={runningNodeId === node.id}
      onCommitTitle={(value) => {
        const title = normalizeCanvasNodeTitle(value, node.title);
        if (title !== node.title) updateNodeTitle(node.id, title);
        return title;
      }}
      onToggleCollapsed={(collapsed) =>
        updateNodeAppearance(node.id, {
          ui: {
            ...node.ui,
            collapsed,
          },
        })
      }
      onChangeColorTag={(colorTag) =>
        updateNodeAppearance(node.id, {
          ui: {
            ...node.ui,
            color_tag: colorTag,
          },
        })
      }
      onCancelPendingChange={() => setPendingConfigChange(null)}
      onConfirmPendingChange={() => {
        if (visiblePendingChange) {
          const currentNode = graph.nodes.find(
            (item) => item.id === visiblePendingChange.nodeId,
          );
          if (currentNode) {
            updateNodeConfig(currentNode.id, {
              ...currentNode.config,
              ...visiblePendingChange.changes,
            });
          }
        }
        setPendingConfigChange(null);
      }}
      onDelete={() => removeNodes([node.id])}
      onRun={() => onRunNode(node.id)}
    />
  );
}

type CanvasAssetKind = "image" | "mask" | "video";

function useCanvasAssetUpload(
  store: CanvasEditorStore,
  selectedNodeId: string | null,
) {
  const [uploading, setUploading] = useState(false);
  const sequenceRef = useRef(0);
  const requestRef = useRef<{
    id: number;
    nodeId: string;
    controller: AbortController;
    assetField: "image_id" | "video_id";
    kind: CanvasAssetKind;
    initialAssetId: unknown;
    initialDisplayName: unknown;
  } | null>(null);

  useEffect(() => {
    const request = requestRef.current;
    if (!request || request.nodeId === selectedNodeId) return;
    requestRef.current = null;
    request.controller.abort();
    setUploading(false);
  }, [selectedNodeId]);

  useEffect(
    () => () => {
      const request = requestRef.current;
      requestRef.current = null;
      request?.controller.abort();
    },
    [],
  );

  const uploadAsset = async (file: File, kind: CanvasAssetKind) => {
    requestRef.current?.controller.abort();
    const initialNode = store
      .getState()
      .graph.nodes.find((item) => item.id === selectedNodeId);
    if (!initialNode) return;
    const assetField = canvasAssetIdField(kind);
    sequenceRef.current += 1;
    const request = {
      id: sequenceRef.current,
      nodeId: initialNode.id,
      controller: new AbortController(),
      assetField,
      kind,
      initialAssetId: initialNode.config[assetField],
      initialDisplayName: initialNode.config.display_name,
    };
    requestRef.current = request;
    setUploading(true);
    try {
      const asset = await uploadCanvasAsset(
        file,
        kind,
        request.controller.signal,
      );
      const state = store.getState();
      if (
        requestRef.current?.id !== request.id ||
        state.selectedNodeId !== request.nodeId
      ) {
        await cleanupStaleCanvasUpload({
          graph: state.graph,
          kind: request.kind,
          uploadedAsset: asset,
          initialAssetId: request.initialAssetId,
        });
        return;
      }
      const node = state.graph.nodes.find((item) => item.id === request.nodeId);
      if (!node) {
        await cleanupStaleCanvasUpload({
          graph: state.graph,
          kind: request.kind,
          uploadedAsset: asset,
          initialAssetId: request.initialAssetId,
        });
        return;
      }
      if (
        !Object.is(
          node.config[request.assetField],
          request.initialAssetId,
        ) ||
        !Object.is(
          node.config.display_name,
          request.initialDisplayName,
        )
      ) {
        await cleanupStaleCanvasUpload({
          graph: state.graph,
          kind: request.kind,
          uploadedAsset: asset,
          initialAssetId: request.initialAssetId,
        });
        toast.info("上传已完成，但节点内容已被修改，未自动覆盖。");
        return;
      }
      state.updateNodeConfig(request.nodeId, {
        ...node.config,
        [request.assetField]: asset.id,
        display_name: file.name,
      });
      toast.success(kind === "video" ? "视频已上传" : "图片已上传");
    } catch (error) {
      if (!request.controller.signal.aborted) {
        toast.error(error instanceof Error ? error.message : "上传失败");
      }
    } finally {
      if (requestRef.current?.id === request.id) {
        requestRef.current = null;
        setUploading(false);
      }
    }
  };

  return {
    uploading,
    uploadImage: (file: File) => {
      const node = store
        .getState()
        .graph.nodes.find((item) => item.id === selectedNodeId);
      return uploadAsset(file, node?.type === "mask_asset" ? "mask" : "image");
    },
    uploadVideo: (file: File) => uploadAsset(file, "video"),
  };
}

async function uploadCanvasAsset(
  file: File,
  kind: CanvasAssetKind,
  signal: AbortSignal,
) {
  if (kind !== "video") {
    const asset = await uploadImage(file, {
      signal,
      purpose: kind === "mask" ? "inpaint_mask" : undefined,
    });
    return { ...asset, created: true };
  }
  return uploadReferenceVideo(file, signal);
}

function canvasAssetIdField(kind: CanvasAssetKind): "image_id" | "video_id" {
  return kind === "video" ? "video_id" : "image_id";
}

function CanvasEdgeInspector({
  document,
  edge,
  graph,
  onRemove,
  onUpdateBinding,
  onUpdateDetails,
}: {
  document: CanvasDocument;
  edge: CanvasEdgeDefinition;
  graph: CanvasDocument["graph"];
  onRemove: (edgeId: string) => void;
  onUpdateBinding: (
    edgeId: string,
    bindingMode: "follow_active" | "pinned",
    pinnedExecutionId?: string | null,
    pinnedOutputIndex?: number | null,
  ) => void;
  onUpdateDetails: (
    edgeId: string,
    details: CanvasEdgeDetailsUpdate,
  ) => void;
}) {
  const source = graph.nodes.find((item) => item.id === edge.source_node_id);
  const target = graph.nodes.find((item) => item.id === edge.target_node_id);
  const sourceSelection = document.selections.find(
    (selection) =>
      selection.node_id === edge.source_node_id &&
      selection.execution_id !== null,
  );
  const roleEditable = edge.data_type === "image" || edge.data_type === "mask";
  const targetPort = target
    ? CANVAS_NODE_SPECS[target.type].inputs.find(
        (port) => port.id === edge.target_handle,
      )
    : undefined;
  const orderedPeers = graph.edges
    .filter(
      (candidate) =>
        candidate.target_node_id === edge.target_node_id &&
        candidate.target_handle === edge.target_handle,
    )
    .sort(
      (left, right) =>
        (left.order ?? 0) - (right.order ?? 0) ||
        left.id.localeCompare(right.id),
    );
  const inputOrder = orderedPeers.findIndex(
    (candidate) => candidate.id === edge.id,
  );
  return (
    <InspectorShell
      eyebrow="连接"
      title={`${source?.title ?? "来源"} → ${target?.title ?? "目标"}`}
    >
      <InspectorSection title="输入绑定">
        <ReadOnlyRow label="类型" value={DATA_TYPE_LABELS[edge.data_type]} />
        <ReadOnlyRow
          label="来源端口"
          value={portLabel(source, "output", edge.source_handle)}
        />
        <ReadOnlyRow
          label="目标端口"
          value={portLabel(target, "input", edge.target_handle)}
        />
        {targetPort?.multiple && inputOrder >= 0 && orderedPeers.length > 1 ? (
          <EdgeOrderControl
            index={inputOrder}
            total={orderedPeers.length}
            onMove={(order) => onUpdateDetails(edge.id, { order })}
          />
        ) : null}
        {roleEditable ? (
          <SelectField
            label="参考角色"
            value={edge.role ?? ""}
            options={EDGE_ROLE_OPTIONS}
            onChange={(value) =>
              onUpdateDetails(edge.id, {
                role: (value || null) as CanvasEdgeRole | null,
                order: edge.order ?? null,
              })
            }
          />
        ) : (
          <ReadOnlyRow label="参考角色" value="不适用" />
        )}
        <div
          className="grid grid-cols-2 gap-2"
          role="group"
          aria-label="输入版本"
        >
          <Button
            size="sm"
            variant={
              edge.binding_mode === "follow_active" ? "primary" : "outline"
            }
            onClick={() => onUpdateBinding(edge.id, "follow_active")}
          >
            跟随当前
          </Button>
          <Button
            size="sm"
            variant={edge.binding_mode === "pinned" ? "primary" : "outline"}
            disabled={!sourceSelection}
            onClick={() => {
              if (!sourceSelection?.execution_id) return;
              onUpdateBinding(
                edge.id,
                "pinned",
                sourceSelection.execution_id,
                sourceSelection.output_index,
              );
            }}
          >
            固定当前版本
          </Button>
        </div>
      </InspectorSection>
      <div className="border-t border-[var(--border)] p-4">
        <Button
          variant="danger"
          fullWidth
          leftIcon={<Trash2 className="h-4 w-4" />}
          onClick={() => onRemove(edge.id)}
        >
          删除连接
        </Button>
      </div>
    </InspectorShell>
  );
}

function EdgeOrderControl({
  index,
  total,
  onMove,
}: {
  index: number;
  total: number;
  onMove: (order: number) => void;
}) {
  return (
    <div className="flex min-h-9 items-center justify-between gap-3">
      <span className="type-body-sm text-[var(--fg-2)]">输入顺序</span>
      <div className="flex items-center gap-1">
        <Button
          size="sm"
          variant="outline"
          className="w-9 px-0 max-sm:w-11"
          aria-label="输入上移"
          title="输入上移"
          disabled={index === 0}
          onClick={() => onMove(index - 1)}
        >
          <ArrowUp className="h-4 w-4" aria-hidden />
        </Button>
        <span className="min-w-12 text-center type-caption tabular-nums text-[var(--fg-1)]">
          {index + 1} / {total}
        </span>
        <Button
          size="sm"
          variant="outline"
          className="w-9 px-0 max-sm:w-11"
          aria-label="输入下移"
          title="输入下移"
          disabled={index === total - 1}
          onClick={() => onMove(index + 1)}
        >
          <ArrowDown className="h-4 w-4" aria-hidden />
        </Button>
      </div>
    </div>
  );
}

function HistoryOutput({
  output,
  index,
  active,
  loading,
  onSelect,
}: CanvasHistoryOutputProps) {
  const Icon = output.type === "image" ? ImageIcon : Video;
  const sources = historyOutputPreviewSources(output);
  const sourceKey = sources.join("\n");
  const [sourceState, setSourceState] = useState({
    key: sourceKey,
    index: 0,
  });
  const sourceIndex = sourceState.key === sourceKey ? sourceState.index : 0;
  const visibleSrc = sources[sourceIndex] ?? null;
  return (
    <div
      style={{ aspectRatio: historyOutputAspectRatio(output) }}
      className="relative min-h-11 w-full overflow-hidden rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--surface-media)]"
    >
      <button
        type="button"
        aria-label={`${active ? "当前" : "选择"}第 ${index + 1} 个${output.type === "image" ? "图片" : "视频"}输出`}
        aria-pressed={active}
        disabled={active || loading}
        onClick={onSelect}
        className="absolute inset-0 h-full w-full disabled:cursor-default"
      >
        {visibleSrc ? (
          // eslint-disable-next-line @next/next/no-img-element -- API-backed execution output.
          <img
            src={visibleSrc}
            alt={`第 ${index + 1} 个${output.type === "image" ? "图片" : "视频"}输出预览`}
            loading="lazy"
            decoding="async"
            className="h-full w-full object-contain"
            onError={() =>
              setSourceState({ key: sourceKey, index: sourceIndex + 1 })
            }
          />
        ) : (
          <span className="grid h-full place-items-center text-[var(--fg-2)]">
            <Icon className="h-5 w-5" aria-hidden />
          </span>
        )}
        {active ? (
          <span className="absolute right-1 top-1 grid h-5 w-5 place-items-center rounded-full bg-[var(--success)] text-[var(--success-on)]">
            <Check className="h-3 w-3" />
          </span>
        ) : loading ? (
          <span className="absolute inset-0 grid place-items-center bg-[var(--surface-scrim)] text-[var(--media-control-fg)]">
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
          </span>
        ) : null}
      </button>
      <CanvasOutputDownloadButton
        output={output}
        title={`画布输出 ${index + 1}`}
        className="absolute bottom-1 left-1 z-10"
      />
    </div>
  );
}

function historyOutputPreviewSources(output: CanvasOutput): string[] {
  if (output.type === "video") {
    return uniqueMediaSources([
      output.poster_url,
      output.preview_url,
      output.video_id ? videoPosterUrl(output.video_id) : null,
    ]);
  }
  return uniqueMediaSources([
    output.image_id ? imageVariantUrl(output.image_id, "display2048") : null,
    output.preview_url,
    output.url,
    output.image_id ? imageBinaryUrl(output.image_id) : null,
  ]);
}

function uniqueMediaSources(
  sources: Array<string | null | undefined>,
): string[] {
  return Array.from(
    new Set(
      sources
        .map((source) => source?.trim() ?? "")
        .filter((source) => source.length > 0),
    ),
  );
}

function historyOutputAspectRatio(output: CanvasOutput): string {
  const width = Number(output.width);
  const height = Number(output.height);
  if (
    Number.isFinite(width) &&
    Number.isFinite(height) &&
    width > 0 &&
    height > 0
  ) {
    return `${width} / ${height}`;
  }
  return output.type === "video" ? "16 / 9" : "1 / 1";
}

function portLabel(
  node: CanvasNodeDefinition | undefined,
  direction: "input" | "output",
  handle: string,
): string {
  if (!node) return "未知端口";
  const spec = CANVAS_NODE_SPECS[node.type];
  const ports = direction === "input" ? spec.inputs : spec.outputs;
  return ports.find((port) => port.id === handle)?.label ?? "未知端口";
}
