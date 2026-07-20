"use client";

import {
  ArrowDown,
  ArrowUp,
  AlignHorizontalJustifyCenter,
  AlignHorizontalJustifyEnd,
  AlignHorizontalJustifyStart,
  AlignHorizontalSpaceBetween,
  AlignVerticalJustifyCenter,
  AlignVerticalJustifyEnd,
  AlignVerticalJustifyStart,
  AlignVerticalSpaceBetween,
  Check,
  Copy,
  Image as ImageIcon,
  LayoutGrid,
  Loader2,
  Play,
  Scan,
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
import {
  canvasExecutionElapsedMs,
  canvasExecutionPrimaryTask,
  canvasExecutionProgressPercent,
  canvasExecutionStageLabel,
  canvasExecutionStatusLabel,
  formatCanvasTaskElapsed,
  isCanvasExecutionActive,
} from "@/lib/canvas/executionPresentation";
import {
  CANVAS_NODE_TITLE_MAX_CHARS,
  normalizeCanvasNodeTitle,
} from "@/lib/canvas/constants";
import {
  canvasVideoCapabilityError,
  validateCanvasNodeExecution,
} from "@/lib/canvas/graph";
import type {
  CanvasDocument,
  CanvasEdgeDefinition,
  CanvasEdgeDetailsUpdate,
  CanvasExecutionTaskDetail,
  CanvasNodeDefinition,
  CanvasNodeExecution,
  CanvasNodeType,
  CanvasOutput,
} from "@/lib/canvas/types";
import {
  CANVAS_NODE_SPECS,
  findMatchingCanvasNodeCatalogItem,
  isCanvasExecutableNodeType,
} from "@/lib/canvas/registry";
import type { CanvasEditorStore } from "@/lib/canvas/store";
import { deleteCanvasUploadedAsset } from "@/lib/api/canvases";
import { useSelectCanvasOutputMutation } from "@/lib/queries/canvases";
import { cn } from "@/lib/utils";
import { Button, Input, toast } from "@/components/ui/primitives";
import {
  ColorSwatchField,
  InlineConfigConfirmation,
  InspectorSection,
  InspectorShell,
  ReadOnlyRow,
  SelectField,
  ToggleField,
} from "./CanvasInspectorFields";
import type { SelectOption } from "./CanvasInspectorFields";
import { CanvasNodeConfigEditor } from "./CanvasNodeConfigEditor";
import { CanvasOutputDownloadButton } from "./CanvasOutputDownloadButton";
import {
  useCanvasStore,
  useCanvasStoreApi,
} from "./CanvasStoreProvider";

type CanvasEdgeRole = NonNullable<CanvasEdgeDefinition["role"]>;

interface PendingConfigChange {
  nodeId: string;
  changes: Record<string, unknown>;
  removedConnections: number;
}

export type CanvasSelectionAlignment =
  | "left"
  | "horizontal-center"
  | "right"
  | "top"
  | "vertical-center"
  | "bottom";

export type CanvasSelectionDistribution = "horizontal" | "vertical";

export interface CanvasInspectorProps {
  document: CanvasDocument;
  onRunNode: (nodeId: string) => void;
  runningNodeId?: string | null;
  onDuplicateSelection?: () => void;
  onAlignSelection?: (alignment: CanvasSelectionAlignment) => void;
  onDistributeSelection?: (
    distribution: CanvasSelectionDistribution,
  ) => void;
  onAutoLayoutSelection?: () => void;
  onFitSelection?: () => void;
}

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
      <BatchInspector
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
    <InspectorShell
      eyebrow={preset?.label ?? CANVAS_NODE_SPECS[node.type].label}
      title={node.title}
    >
      <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto">
        <InspectorSection title="节点">
          <Input
            label="名称"
            defaultValue={node.title}
            key={`${node.id}:${node.title}`}
            maxLength={CANVAS_NODE_TITLE_MAX_CHARS}
            onBlur={(event) => {
              const title = normalizeCanvasNodeTitle(
                event.currentTarget.value,
                node.title,
              );
              event.currentTarget.value = title;
              if (title !== node.title) updateNodeTitle(node.id, title);
            }}
          />
          <ToggleField
            label="折叠节点"
            checked={node.ui.collapsed === true}
            onChange={(collapsed) =>
              updateNodeAppearance(node.id, {
                ui: {
                  ...node.ui,
                  collapsed,
                },
              })
            }
          />
          <ColorSwatchField
            value={node.ui.color_tag ?? null}
            onChange={(colorTag) =>
              updateNodeAppearance(node.id, {
                ui: {
                  ...node.ui,
                  color_tag: colorTag,
                },
              })
            }
          />
        </InspectorSection>

        <CanvasNodeConfigEditor
          node={node}
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
        />

        {visiblePendingChange ? (
          <InlineConfigConfirmation
            removedConnections={visiblePendingChange.removedConnections}
            onCancel={() => setPendingConfigChange(null)}
            onConfirm={() => {
              const currentNode = graph.nodes.find(
                (item) => item.id === visiblePendingChange.nodeId,
              );
              if (currentNode) {
                updateNodeConfig(currentNode.id, {
                  ...currentNode.config,
                  ...visiblePendingChange.changes,
                });
              }
              setPendingConfigChange(null);
            }}
          />
        ) : null}

        {executions.length > 0 ? (
          <ExecutionHistory executions={executions} document={document} />
        ) : null}
      </div>

      <footer className="mobile-dialog-footer grid shrink-0 gap-2 border-t border-[var(--border)] bg-[var(--bg-1)]/92 p-3">
        {runDisabledReason ? (
          <p
            role="alert"
            className="type-caption text-[var(--danger-fg)]"
          >
            {runDisabledReason}
          </p>
        ) : null}
        <div className="flex items-center justify-between gap-3">
          <Button
            variant="ghost"
            leftIcon={<Trash2 className="h-4 w-4" />}
            onClick={() => removeNodes([node.id])}
            className="text-[var(--danger-fg)] hover:bg-[var(--danger-soft)]"
          >
            删除
          </Button>
          {canRun ? (
            <Button
              variant="primary"
              loading={runningNodeId === node.id}
              disabled={Boolean(runDisabledReason)}
              leftIcon={<Play className="h-4 w-4" />}
              onClick={() => onRunNode(node.id)}
            >
              运行节点
            </Button>
          ) : (
            <Button variant="secondary" disabled>
              无需运行
            </Button>
          )}
        </div>
      </footer>
    </InspectorShell>
  );
}

function inspectorRunDisabledReason(
  graph: CanvasDocument["graph"],
  node: CanvasNodeDefinition,
): string | null {
  if (!isCanvasExecutableNodeType(node.type)) return null;
  const validation = validateCanvasNodeExecution(graph, node.id);
  return validation.valid ? null : validation.reason;
}

function inspectorVideoRunDisabledReason(
  graph: CanvasDocument["graph"],
  node: CanvasNodeDefinition,
  options: Awaited<ReturnType<typeof fetchVideoOptions>> | undefined,
  loading: boolean,
  error: string | null,
): string | null {
  if (CANVAS_NODE_SPECS[node.type].family !== "video") return null;
  if (loading) return "正在加载视频能力";
  if (error) return error;
  if (!options) return "视频能力尚未加载";
  return canvasVideoCapabilityError(node, options, graph);
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
        await cleanupStaleCanvasAsset(
          state.graph,
          request.kind,
          asset.id,
          asset.created,
          request.initialAssetId,
        );
        return;
      }
      const node = state.graph.nodes.find((item) => item.id === request.nodeId);
      if (!node) {
        await cleanupStaleCanvasAsset(
          state.graph,
          request.kind,
          asset.id,
          asset.created,
          request.initialAssetId,
        );
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
        await cleanupStaleCanvasAsset(
          state.graph,
          request.kind,
          asset.id,
          asset.created,
          request.initialAssetId,
        );
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

function isCanvasAssetReferenced(
  graph: CanvasDocument["graph"],
  kind: CanvasAssetKind,
  assetId: string,
): boolean {
  const field = canvasAssetIdField(kind);
  return graph.nodes.some((node) => node.config[field] === assetId);
}

function shouldCleanupStaleCanvasAsset(
  graph: CanvasDocument["graph"],
  kind: CanvasAssetKind,
  assetId: string,
  createdByRequest: boolean,
  initialAssetId: unknown,
): boolean {
  return (
    createdByRequest &&
    assetId.trim().length > 0 &&
    !Object.is(assetId, initialAssetId) &&
    !isCanvasAssetReferenced(graph, kind, assetId)
  );
}

async function cleanupStaleCanvasAsset(
  graph: CanvasDocument["graph"],
  kind: CanvasAssetKind,
  assetId: string,
  createdByRequest: boolean,
  initialAssetId: unknown,
): Promise<void> {
  if (
    !shouldCleanupStaleCanvasAsset(
      graph,
      kind,
      assetId,
      createdByRequest,
      initialAssetId,
    )
  ) {
    return;
  }
  try {
    await deleteCanvasUploadedAsset(
      kind === "video" ? "video" : "image",
      assetId,
    );
  } catch {
    // The server keeps a referenced asset; cleanup is best effort.
  }
}

function queryErrorMessage(
  isError: boolean,
  error: unknown,
  fallback: string,
): string | null {
  if (!isError) return null;
  return error instanceof Error ? error.message : fallback;
}

function canvasNodePreset(node: CanvasNodeDefinition) {
  return findMatchingCanvasNodeCatalogItem(node);
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

function BatchInspector({
  nodes,
  onDuplicateSelection,
  onAlignSelection,
  onDistributeSelection,
  onAutoLayoutSelection,
  onFitSelection,
  onDeleteSelection,
}: {
  nodes: CanvasNodeDefinition[];
  onDuplicateSelection?: () => void;
  onAlignSelection?: (alignment: CanvasSelectionAlignment) => void;
  onDistributeSelection?: (
    distribution: CanvasSelectionDistribution,
  ) => void;
  onAutoLayoutSelection?: () => void;
  onFitSelection?: () => void;
  onDeleteSelection: () => void;
}) {
  const hasGeneralActions =
    onDuplicateSelection || onAutoLayoutSelection || onFitSelection;
  return (
    <InspectorShell eyebrow="批量检查器" title={`已选择 ${nodes.length} 个节点`}>
      <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto">
        <InspectorSection title="选择摘要">
          <p className="type-body-sm text-[var(--fg-1)]">
            {selectionSummary(nodes)}
          </p>
        </InspectorSection>

        {hasGeneralActions ? (
          <InspectorSection title="批量操作">
            <div
              className="grid grid-cols-2 gap-2"
              role="group"
              aria-label="批量节点操作"
            >
              {onDuplicateSelection ? (
                <Button
                  size="sm"
                  variant="outline"
                  leftIcon={<Copy className="h-4 w-4" aria-hidden />}
                  onClick={onDuplicateSelection}
                >
                  复制节点
                </Button>
              ) : null}
              {onAutoLayoutSelection ? (
                <Button
                  size="sm"
                  variant="outline"
                  leftIcon={<LayoutGrid className="h-4 w-4" aria-hidden />}
                  onClick={onAutoLayoutSelection}
                >
                  自动布局
                </Button>
              ) : null}
              {onFitSelection ? (
                <Button
                  size="sm"
                  variant="outline"
                  leftIcon={<Scan className="h-4 w-4" aria-hidden />}
                  onClick={onFitSelection}
                >
                  适应选择
                </Button>
              ) : null}
            </div>
          </InspectorSection>
        ) : null}

        {onAlignSelection ? (
          <InspectorSection title="对齐">
            <div
              className="grid grid-cols-3 gap-2"
              role="group"
              aria-label="节点对齐方式"
            >
              <BatchLayoutButton
                label="左对齐"
                icon={<AlignHorizontalJustifyStart className="h-4 w-4" />}
                onClick={() => onAlignSelection("left")}
              />
              <BatchLayoutButton
                label="水平居中"
                icon={<AlignHorizontalJustifyCenter className="h-4 w-4" />}
                onClick={() => onAlignSelection("horizontal-center")}
              />
              <BatchLayoutButton
                label="右对齐"
                icon={<AlignHorizontalJustifyEnd className="h-4 w-4" />}
                onClick={() => onAlignSelection("right")}
              />
              <BatchLayoutButton
                label="顶部对齐"
                icon={<AlignVerticalJustifyStart className="h-4 w-4" />}
                onClick={() => onAlignSelection("top")}
              />
              <BatchLayoutButton
                label="垂直居中"
                icon={<AlignVerticalJustifyCenter className="h-4 w-4" />}
                onClick={() => onAlignSelection("vertical-center")}
              />
              <BatchLayoutButton
                label="底部对齐"
                icon={<AlignVerticalJustifyEnd className="h-4 w-4" />}
                onClick={() => onAlignSelection("bottom")}
              />
            </div>
          </InspectorSection>
        ) : null}

        {onDistributeSelection ? (
          <InspectorSection title="均匀分布">
            <div
              className="grid grid-cols-2 gap-2"
              role="group"
              aria-label="节点分布方式"
            >
              <Button
                size="sm"
                variant="outline"
                leftIcon={
                  <AlignHorizontalSpaceBetween
                    className="h-4 w-4"
                    aria-hidden
                  />
                }
                onClick={() => onDistributeSelection("horizontal")}
              >
                水平分布
              </Button>
              <Button
                size="sm"
                variant="outline"
                leftIcon={
                  <AlignVerticalSpaceBetween
                    className="h-4 w-4"
                    aria-hidden
                  />
                }
                onClick={() => onDistributeSelection("vertical")}
              >
                垂直分布
              </Button>
            </div>
          </InspectorSection>
        ) : null}
      </div>

      <footer className="mobile-dialog-footer shrink-0 border-t border-[var(--border)] bg-[var(--bg-1)]/92 p-3">
        <Button
          variant="danger"
          fullWidth
          leftIcon={<Trash2 className="h-4 w-4" aria-hidden />}
          onClick={onDeleteSelection}
        >
          删除所选
        </Button>
      </footer>
    </InspectorShell>
  );
}

function BatchLayoutButton({
  label,
  icon,
  onClick,
}: {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <Button
      size="sm"
      variant="outline"
      className="min-w-0 px-2 text-[11px]"
      leftIcon={<span aria-hidden>{icon}</span>}
      onClick={onClick}
    >
      {label}
    </Button>
  );
}

function ExecutionHistory({
  executions,
  document,
}: {
  executions: CanvasNodeExecution[];
  document: CanvasDocument;
}) {
  const selectedNodeId = useCanvasStore((state) => state.selectedNodeId);
  const selectOutput = useSelectCanvasOutputMutation(document.id);
  const current = document.selections.find(
    (selection) => selection.node_id === selectedNodeId,
  );
  return (
    <InspectorSection title="历史输出">
      <div className="grid gap-2">
        {executions.map((execution) => (
          <div key={execution.id} className="border-b border-[var(--border-subtle)] pb-3 last:border-0">
            <div className="flex items-center justify-between gap-2">
              <span
                className={cn(
                  "type-caption font-medium",
                  execution.status === "partial_failed"
                    ? "text-[var(--warning-fg)]"
                    : "text-[var(--fg-2)]",
                )}
              >
                {canvasExecutionStatusLabel(execution.status)}
              </span>
              <span className="type-caption text-[var(--fg-3)]">
                {execution.created_at
                  ? new Date(execution.created_at).toLocaleString("zh-CN")
                  : ""}
              </span>
            </div>
            <ExecutionTaskDetails execution={execution} />
            {execution.error_message ||
            canvasExecutionPrimaryTask(execution)?.error_message ? (
              <p
                role={execution.status === "partial_failed" ? "status" : "alert"}
                className={cn(
                  "mt-2 type-caption",
                  execution.status === "partial_failed"
                    ? "text-[var(--warning-fg)]"
                    : "text-[var(--danger-fg)]",
                )}
              >
                {execution.error_message ??
                  canvasExecutionPrimaryTask(execution)?.error_message}
              </p>
            ) : null}
            {execution.outputs.length > 0 ? (
              <div className="mt-2 grid grid-cols-3 gap-2">
                {execution.outputs.map((output, index) => (
                  <HistoryOutput
                    key={`${execution.id}:${index}`}
                    output={output}
                    index={index}
                    active={
                      current?.execution_id === execution.id &&
                      current.output_index === index
                    }
                    loading={
                      selectOutput.isPending &&
                      selectOutput.variables?.nodeId === execution.node_id
                    }
                    onSelect={() =>
                      selectOutput.mutate(
                        {
                          nodeId: execution.node_id,
                          executionId: execution.id,
                          outputIndex: index,
                          selectionRevision: current?.revision,
                        },
                        {
                          onError: (error) => toast.error(error.message),
                        },
                      )
                    }
                  />
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </InspectorSection>
  );
}

function ExecutionTaskDetails({
  execution,
}: {
  execution: CanvasNodeExecution;
}) {
  const task = canvasExecutionPrimaryTask(execution);
  const active = isCanvasExecutionActive(execution);
  const [detailsOpen, setDetailsOpen] = useState(active);
  if (!task && !active) return null;
  const progress = canvasExecutionProgressPercent(execution);
  const stage = canvasExecutionStageLabel(execution);
  const elapsed = formatCanvasTaskElapsed(canvasExecutionElapsedMs(execution));
  const rows = task ? executionTaskRows(task, elapsed) : [];
  return (
    <div className="mt-2 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/56 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="type-caption font-medium text-[var(--fg-1)]">
          {stage}
        </span>
        <span className="type-mono-meta tabular-nums text-[var(--fg-2)]">
          {progress !== null
            ? `${progress}%`
            : elapsed
              ? `已用 ${elapsed}`
              : "进行中"}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={`${stage}进度`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress ?? undefined}
        className="mt-2 h-1.5 overflow-hidden rounded-full bg-[var(--bg-3)]"
      >
        <span
          className={cn(
            "block h-full rounded-full bg-[var(--accent)]",
            progress === null
              ? "w-1/3 animate-pulse motion-reduce:animate-none"
              : "w-full origin-left transition-transform duration-[var(--dur-base)] ease-[var(--ease-develop)]",
          )}
          style={
            progress === null
              ? undefined
              : { transform: `scaleX(${progress / 100})` }
          }
        />
      </div>
      {rows.length > 0 ? (
        <details
          className="mt-2"
          open={detailsOpen}
          onToggle={(event) => setDetailsOpen(event.currentTarget.open)}
        >
          <summary className="cursor-pointer type-caption text-[var(--fg-2)]">
            任务详情
          </summary>
          <dl className="mt-2 grid grid-cols-[68px_minmax(0,1fr)] gap-x-2 gap-y-1.5 type-caption">
            {rows.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="text-[var(--fg-3)]">{label}</dt>
                <dd className="min-w-0 break-words text-[var(--fg-1)]">
                  {value}
                </dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </div>
  );
}

function executionTaskRows(
  task: CanvasExecutionTaskDetail,
  elapsed: string | null,
): Array<[string, string]> {
  const taskId =
    task.video_generation_id ??
    task.generation_id ??
    task.completion_id ??
    task.id;
  const provider = [task.provider_name, task.provider_kind]
    .filter(Boolean)
    .join(" · ");
  const output = [
    task.resolution,
    task.duration_s != null ? `${task.duration_s} 秒` : null,
    task.aspect_ratio,
    task.size_requested,
  ]
    .filter(Boolean)
    .join(" · ");
  const rows: Array<[string, string]> = [
    ["任务 ID", taskId],
    ["类型", canvasTaskKindLabel(task.kind)],
    ["模型", task.model ?? ""],
    ["供应商", provider],
    ["模式", canvasTaskActionLabel(task.action)],
    ["规格", output],
    [
      "音频",
      task.generate_audio == null
        ? ""
        : task.generate_audio
          ? "生成音频"
          : "静音",
    ],
    ["尝试", task.attempt == null ? "" : String(task.attempt + 1)],
    ["耗时", elapsed ?? ""],
    ["更新时间", formatCanvasTaskTime(task.updated_at)],
  ];
  return rows.filter((row) => Boolean(row[1]));
}

function canvasTaskKindLabel(kind: string): string {
  return (
    {
      generation: "图片生成",
      completion: "文本处理",
      video_generation: "视频生成",
    }[kind] ?? kind
  );
}

function canvasTaskActionLabel(action: string | null | undefined): string {
  if (!action) return "";
  return (
    {
      t2v: "文生视频",
      i2v: "图生视频",
      reference: "参考生成",
      generate: "生成",
      edit: "编辑",
    }[action] ?? action
  );
}

function formatCanvasTaskTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN");
}

function HistoryOutput({
  output,
  index,
  active,
  loading,
  onSelect,
}: {
  output: CanvasOutput;
  index: number;
  active: boolean;
  loading: boolean;
  onSelect: () => void;
}) {
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

function selectionSummary(nodes: CanvasNodeDefinition[]): string {
  const counts = new Map<CanvasNodeType, number>();
  for (const node of nodes) {
    counts.set(node.type, (counts.get(node.type) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([type, count]) => `${CANVAS_NODE_SPECS[type].label} ${count} 个`)
    .join("，");
}

function incompatibleVideoConnectionCount(
  graph: CanvasDocument["graph"],
  node: CanvasNodeDefinition,
  nextConfig: Record<string, unknown>,
): number {
  if (node.type !== "video_generate") return 0;
  const currentMode = String(node.config.mode ?? "t2v");
  const nextMode = String(nextConfig.mode ?? "t2v");
  if (currentMode === nextMode) return 0;
  const blocked =
    nextMode === "t2v"
      ? new Set(["first_frame", "reference_images", "reference_videos"])
      : nextMode === "i2v"
        ? new Set(["reference_images", "reference_videos"])
        : new Set(["first_frame"]);
  return graph.edges.filter(
    (edge) =>
      edge.target_node_id === node.id && blocked.has(edge.target_handle),
  ).length;
}
