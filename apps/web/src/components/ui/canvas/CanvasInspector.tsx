"use client";

import {
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
  Upload,
  Video,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { uploadImage } from "@/lib/apiClient";
import { imageVariantUrl, videoPosterUrl } from "@/lib/apiClient";
import type {
  CanvasDocument,
  CanvasEdgeDefinition,
  CanvasEdgeDetailsUpdate,
  CanvasNodeDefinition,
  CanvasNodeExecution,
  CanvasNodeType,
  CanvasOutput,
} from "@/lib/canvas/types";
import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import { useSelectCanvasOutputMutation } from "@/lib/queries/canvases";
import { Button, Input, toast } from "@/components/ui/primitives";
import {
  useCanvasStore,
  useCanvasStoreApi,
} from "./CanvasStoreProvider";

const SELECT_CLASS =
  "h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 type-body-sm text-[var(--fg-0)] focus:border-[var(--accent)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-soft)] max-sm:min-h-11 max-sm:text-base";

type SelectOption = {
  value: string;
  label: string;
};

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

const EXECUTION_STATUS_LABELS: Record<
  CanvasNodeExecution["status"],
  string
> = {
  pending: "待处理",
  ready: "已就绪",
  queued: "排队中",
  running: "运行中",
  reconciling: "同步结果中",
  canceling: "正在取消",
  succeeded: "已成功",
  partial_failed: "部分失败",
  failed: "已失败",
  blocked: "已阻塞",
  canceled: "已取消",
  skipped: "已跳过",
  reused: "已复用",
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

const IMAGE_ASPECT_OPTIONS: readonly SelectOption[] = [
  { value: "1:1", label: "方形 1:1" },
  { value: "4:5", label: "竖版 4:5" },
  { value: "3:4", label: "竖版 3:4" },
  { value: "2:3", label: "竖版 2:3" },
  { value: "7:10", label: "竖版 7:10" },
  { value: "9:16", label: "竖屏 9:16" },
  { value: "3:2", label: "横版 3:2" },
  { value: "4:3", label: "横版 4:3" },
  { value: "10:7", label: "横版 10:7" },
  { value: "16:9", label: "宽屏 16:9" },
  { value: "21:9", label: "超宽 21:9" },
  { value: "9:21", label: "超长竖屏 9:21" },
];

const IMAGE_QUALITY_OPTIONS: readonly SelectOption[] = [
  { value: "1k", label: "1K" },
  { value: "2k", label: "2K" },
  { value: "4k", label: "4K" },
  { value: "standard", label: "标准（兼容旧配置）" },
  { value: "high", label: "高质量（兼容旧配置）" },
];

const RENDER_QUALITY_OPTIONS: readonly SelectOption[] = [
  { value: "auto", label: "自动" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

const SIZE_MODE_OPTIONS: readonly SelectOption[] = [
  { value: "auto", label: "自动尺寸" },
  { value: "fixed", label: "固定像素" },
];

const IMAGE_FORMAT_OPTIONS: readonly SelectOption[] = [
  { value: "webp", label: "WebP（推荐）" },
  { value: "jpeg", label: "JPEG（兼容性高）" },
  { value: "png", label: "PNG（无损）" },
];

const IMAGE_BACKGROUND_OPTIONS: readonly SelectOption[] = [
  { value: "auto", label: "自动" },
  { value: "opaque", label: "不透明" },
  { value: "transparent", label: "透明" },
];

const IMAGE_MODERATION_OPTIONS: readonly SelectOption[] = [
  { value: "auto", label: "自动审核" },
  { value: "low", label: "低强度审核" },
];

const VIDEO_MODE_OPTIONS: readonly SelectOption[] = [
  { value: "t2v", label: "文生视频" },
  { value: "i2v", label: "首帧生视频" },
  { value: "reference", label: "参考媒体生成" },
];

const VIDEO_RESOLUTION_OPTIONS: readonly SelectOption[] = [
  { value: "480p", label: "480P" },
  { value: "720p", label: "720P" },
  { value: "1080p", label: "1080P" },
  { value: "4k", label: "4K" },
];

const VIDEO_ASPECT_OPTIONS: readonly SelectOption[] = [
  { value: "adaptive", label: "自适应" },
  { value: "16:9", label: "宽屏 16:9" },
  { value: "21:9", label: "超宽 21:9" },
  { value: "4:3", label: "横版 4:3" },
  { value: "1:1", label: "方形 1:1" },
  { value: "3:4", label: "竖版 3:4" },
  { value: "9:16", label: "竖屏 9:16" },
];

const NODE_COLOR_OPTIONS = [
  { value: null, label: "无颜色", color: "var(--bg-3)" },
  { value: "accent", label: "琥珀色", color: "var(--accent)" },
  { value: "success", label: "绿色", color: "var(--success)" },
  { value: "info", label: "蓝色", color: "var(--info)" },
  { value: "danger", label: "红色", color: "var(--danger)" },
] as const;

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
  const store = useCanvasStoreApi();
  const graph = useCanvasStore((state) => state.graph);
  const selectedNodeId = useCanvasStore((state) => state.selectedNodeId);
  const selectedNodeIds = useCanvasStore((state) => state.selectedNodeIds);
  const selectedEdgeId = useCanvasStore((state) => state.selectedEdgeId);
  const updateNodeConfig = useCanvasStore((state) => state.updateNodeConfig);
  const updateNodeTitle = useCanvasStore((state) => state.updateNodeTitle);
  const removeNodes = useCanvasStore((state) => state.removeNodes);
  const removeEdges = useCanvasStore((state) => state.removeEdges);
  const updateEdgeBinding = useCanvasStore(
    (state) => state.updateEdgeBinding,
  );
  const updateNodeAppearance = useCanvasStore(
    (state) => state.updateNodeAppearance,
  );
  const updateEdgeDetails = useCanvasStore(
    (state) => state.updateEdgeDetails,
  );
  const [uploading, setUploading] = useState(false);
  const uploadSequenceRef = useRef(0);
  const uploadRequestRef = useRef<{
    id: number;
    nodeId: string;
    controller: AbortController;
  } | null>(null);
  const [pendingConfigChange, setPendingConfigChange] =
    useState<PendingConfigChange | null>(null);

  const node = graph.nodes.find((item) => item.id === selectedNodeId) ?? null;
  const edge = graph.edges.find((item) => item.id === selectedEdgeId) ?? null;
  const selectedNodes = useMemo(() => {
    const selected = new Set(selectedNodeIds);
    return graph.nodes.filter((item) => selected.has(item.id));
  }, [graph.nodes, selectedNodeIds]);
  const executions = useMemo(
    () =>
      document.recent_executions.filter(
        (execution) => execution.node_id === selectedNodeId,
      ),
    [document.recent_executions, selectedNodeId],
  );

  useEffect(() => {
    const request = uploadRequestRef.current;
    if (!request || request.nodeId === selectedNodeId) return;
    uploadRequestRef.current = null;
    request.controller.abort();
    setUploading(false);
  }, [selectedNodeId]);

  useEffect(
    () => () => {
      const request = uploadRequestRef.current;
      uploadRequestRef.current = null;
      request?.controller.abort();
    },
    [],
  );

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
  const canRun = node.type === "image_generate" || node.type === "video_generate";
  const visiblePendingChange =
    pendingConfigChange?.nodeId === node.id ? pendingConfigChange : null;

  return (
    <InspectorShell eyebrow={CANVAS_NODE_SPECS[node.type].label} title={node.title}>
      <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto">
        <InspectorSection title="节点">
          <Input
            label="名称"
            defaultValue={node.title}
            key={`${node.id}:${node.title}`}
            maxLength={80}
            onBlur={(event) => updateNodeTitle(node.id, event.currentTarget.value)}
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

        <NodeConfigEditor
          node={node}
          patch={patch}
          uploading={uploading}
          onUpload={async (file) => {
            uploadRequestRef.current?.controller.abort();
            uploadSequenceRef.current += 1;
            const request = {
              id: uploadSequenceRef.current,
              nodeId: node.id,
              controller: new AbortController(),
            };
            uploadRequestRef.current = request;
            setUploading(true);
            try {
              const image = await uploadImage(file, {
                signal: request.controller.signal,
              });
              if (
                uploadRequestRef.current?.id !== request.id ||
                store.getState().selectedNodeId !== request.nodeId
              ) {
                return;
              }
              const currentNode = store
                .getState()
                .graph.nodes.find((item) => item.id === request.nodeId);
              if (!currentNode) return;
              updateNodeConfig(request.nodeId, {
                ...currentNode.config,
                image_id: image.id,
                display_name: file.name,
              });
              toast.success("图片已上传");
            } catch (error) {
              if (request.controller.signal.aborted) return;
              toast.error(error instanceof Error ? error.message : "上传失败");
            } finally {
              if (uploadRequestRef.current?.id === request.id) {
                uploadRequestRef.current = null;
                setUploading(false);
              }
            }
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

      <footer className="mobile-dialog-footer grid shrink-0 grid-cols-2 gap-2 border-t border-[var(--border)] bg-[var(--bg-1)]/92 p-3">
        <Button
          variant="danger"
          leftIcon={<Trash2 className="h-4 w-4" />}
          onClick={() => removeNodes([node.id])}
        >
          删除
        </Button>
        {canRun ? (
          <Button
            variant="primary"
            loading={runningNodeId === node.id}
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
      </footer>
    </InspectorShell>
  );
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

interface NodeConfigEditorProps {
  node: CanvasNodeDefinition;
  patch: (next: Record<string, unknown>) => void;
  uploading: boolean;
  onUpload: (file: File) => Promise<void>;
}

function NodeConfigEditor(props: NodeConfigEditorProps) {
  const Editor = CONFIG_EDITORS[props.node.type];
  return <Editor {...props} />;
}

function NoAdditionalConfig() {
  return null;
}

function ImageAssetConfig({
  node,
  patch,
  uploading,
  onUpload,
}: NodeConfigEditorProps) {
  return (
    <InspectorSection title="图片素材">
      <Input
        label="图片 ID"
        value={String(node.config.image_id ?? "")}
        onChange={(event) => patch({ image_id: event.currentTarget.value })}
      />
      <label className="inline-flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 type-body-sm text-[var(--fg-0)] transition-colors hover:bg-[var(--bg-3)]">
        {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
        {uploading ? "上传中" : "上传图片"}
        <input
          type="file"
          accept="image/png,image/jpeg,image/webp"
          className="sr-only"
          disabled={uploading}
          onChange={(event) => {
            const file = event.currentTarget.files?.[0];
            if (file) void onUpload(file);
          }}
        />
      </label>
    </InspectorSection>
  );
}

function VideoAssetConfig({ node, patch }: NodeConfigEditorProps) {
  return (
    <InspectorSection title="视频素材">
      <Input
        label="视频 ID"
        value={String(node.config.video_id ?? "")}
        onChange={(event) => patch({ video_id: event.currentTarget.value })}
      />
    </InspectorSection>
  );
}

function ImageGenerateConfig({ node, patch }: NodeConfigEditorProps) {
  const sizeMode = String(node.config.size_mode ?? "auto");
  const outputFormat = String(node.config.output_format ?? "webp");
  const compression =
    typeof node.config.output_compression === "number"
      ? node.config.output_compression
      : null;
  return (
    <>
      <InspectorSection title="生成参数">
        <SelectField
          label="比例"
          value={String(node.config.aspect_ratio ?? "1:1")}
          options={IMAGE_ASPECT_OPTIONS}
          onChange={(value) => patch({ aspect_ratio: value })}
        />
        <SelectField
          label="输出尺寸"
          value={String(node.config.quality ?? "2k").toLowerCase()}
          options={IMAGE_QUALITY_OPTIONS}
          onChange={(value) => patch({ quality: value })}
        />
        <SelectField
          label="尺寸模式"
          value={sizeMode}
          options={SIZE_MODE_OPTIONS}
          onChange={(value) =>
            patch({
              size_mode: value,
              fixed_size:
                value === "fixed" ? node.config.fixed_size ?? "" : null,
            })
          }
        />
        {sizeMode === "fixed" ? (
          <Input
            label="固定尺寸"
            value={String(node.config.fixed_size ?? "")}
            placeholder="例如 1536x1024"
            maxLength={32}
            onChange={(event) =>
              patch({ fixed_size: event.currentTarget.value || null })
            }
          />
        ) : null}
        <SelectField
          label="渲染质量"
          value={String(node.config.render_quality ?? "high")}
          options={RENDER_QUALITY_OPTIONS}
          onChange={(value) => patch({ render_quality: value })}
        />
        <RangeField
          label="数量"
          value={Number(node.config.count ?? 1)}
          min={1}
          max={10}
          onChange={(value) => patch({ count: value })}
        />
        <ToggleField
          label="快速模式"
          checked={node.config.fast !== false}
          onChange={(checked) => patch({ fast: checked })}
        />
      </InspectorSection>

      <InspectorSection title="输出设置">
        <SelectField
          label="图片格式"
          value={outputFormat}
          options={IMAGE_FORMAT_OPTIONS}
          onChange={(value) =>
            patch({
              output_format: value,
              ...(value === "png" ? { output_compression: null } : {}),
            })
          }
        />
        <SelectField
          label="背景"
          value={String(node.config.background ?? "auto")}
          options={IMAGE_BACKGROUND_OPTIONS}
          onChange={(value) =>
            patch(
              value === "transparent"
                ? {
                    background: value,
                    output_format: "png",
                    output_compression: null,
                  }
                : { background: value },
            )
          }
        />
        {outputFormat === "png" ? (
          <p className="type-caption text-[var(--fg-2)]">
            PNG 使用无损输出，不提供压缩质量设置。
          </p>
        ) : (
          <>
            <ToggleField
              label="自定义压缩"
              checked={compression !== null}
              onChange={(checked) =>
                patch({ output_compression: checked ? 90 : null })
              }
            />
            {compression !== null ? (
              <RangeField
                label="压缩质量"
                value={compression}
                min={0}
                max={100}
                suffix="%"
                onChange={(value) => patch({ output_compression: value })}
              />
            ) : null}
          </>
        )}
        <SelectField
          label="内容审核"
          value={String(node.config.moderation ?? "low")}
          options={IMAGE_MODERATION_OPTIONS}
          onChange={(value) => patch({ moderation: value })}
        />
      </InspectorSection>
    </>
  );
}

function VideoGenerateConfig({ node, patch }: NodeConfigEditorProps) {
  return (
    <InspectorSection title="生成参数">
      <SelectField
        label="模式"
        value={String(node.config.mode ?? "t2v")}
        options={VIDEO_MODE_OPTIONS}
        onChange={(value) => patch({ mode: value })}
      />
      <Input
        label="模型"
        value={String(node.config.model ?? "")}
        placeholder="使用系统默认"
        onChange={(event) =>
          patch({ model: event.currentTarget.value || null })
        }
      />
      <RangeField
        label="时长"
        value={Number(node.config.duration_s ?? 5)}
        min={3}
        max={15}
        suffix="秒"
        onChange={(value) => patch({ duration_s: value })}
      />
      <SelectField
        label="分辨率"
        value={String(node.config.resolution ?? "720p")}
        options={VIDEO_RESOLUTION_OPTIONS}
        onChange={(value) => patch({ resolution: value })}
      />
      <SelectField
        label="比例"
        value={String(node.config.aspect_ratio ?? "16:9")}
        options={VIDEO_ASPECT_OPTIONS}
        onChange={(value) => patch({ aspect_ratio: value })}
      />
      <Input
        label="种子"
        type="number"
        min={0}
        max={4_294_967_295}
        step={1}
        value={
          typeof node.config.seed === "number"
            ? String(node.config.seed)
            : ""
        }
        hint="留空时每次随机生成"
        onChange={(event) => {
          const raw = event.currentTarget.value;
          if (!raw) {
            patch({ seed: null });
            return;
          }
          const seed = Number(raw);
          if (
            Number.isInteger(seed) &&
            seed >= 0 &&
            seed <= 4_294_967_295
          ) {
            patch({ seed });
          }
        }}
      />
      <ToggleField
        label="生成音频"
        checked={node.config.generate_audio === true}
        onChange={(checked) => patch({ generate_audio: checked })}
      />
      <ToggleField
        label="水印"
        checked={node.config.watermark === true}
        onChange={(checked) => patch({ watermark: checked })}
      />
    </InspectorSection>
  );
}

function FrameConfig({ node, patch }: NodeConfigEditorProps) {
  return (
    <InspectorSection title="画框">
      <p className="type-body-sm text-[var(--fg-2)]">
        画框名称使用上方节点名称。
      </p>
      <ToggleField
        label="运行视图隐藏"
        checked={node.config.hidden_in_run === true}
        onChange={(checked) => patch({ hidden_in_run: checked })}
      />
    </InspectorSection>
  );
}

function DeliveryConfig() {
  return (
    <InspectorSection title="交付">
      <p className="type-body-sm text-[var(--fg-2)]">
        连接到此节点的图片与视频会作为最终交付展示。
      </p>
    </InspectorSection>
  );
}

const CONFIG_EDITORS: Record<
  CanvasNodeType,
  React.ComponentType<NodeConfigEditorProps>
> = {
  prompt: NoAdditionalConfig,
  note: NoAdditionalConfig,
  image_asset: ImageAssetConfig,
  video_asset: VideoAssetConfig,
  image_generate: ImageGenerateConfig,
  video_generate: VideoGenerateConfig,
  frame: FrameConfig,
  delivery: DeliveryConfig,
};

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
              <span className="type-caption font-medium text-[var(--fg-2)]">
                {EXECUTION_STATUS_LABELS[execution.status]}
              </span>
              <span className="type-caption text-[var(--fg-3)]">
                {execution.created_at ? new Date(execution.created_at).toLocaleString("zh-CN") : ""}
              </span>
            </div>
            {execution.error_message ? (
              <p role="alert" className="mt-2 type-caption text-[var(--danger-fg)]">
                {execution.error_message}
              </p>
            ) : null}
            <div className="mt-2 grid grid-cols-3 gap-2">
              {execution.outputs.map((output, index) => (
                <HistoryOutput
                  key={`${execution.id}:${index}`}
                  output={output}
                  active={
                    current?.execution_id === execution.id &&
                    current.output_index === index
                  }
                  loading={
                    selectOutput.isPending &&
                    selectOutput.variables?.executionId === execution.id &&
                    selectOutput.variables.outputIndex === index
                  }
                  onSelect={() =>
                    selectOutput.mutate(
                      {
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
          </div>
        ))}
      </div>
    </InspectorSection>
  );
}

function HistoryOutput({
  output,
  active,
  loading,
  onSelect,
}: {
  output: CanvasOutput;
  active: boolean;
  loading: boolean;
  onSelect: () => void;
}) {
  const Icon = output.type === "image" ? ImageIcon : Video;
  const src = historyOutputPreviewSource(output);
  return (
    <button
      type="button"
      aria-label={active ? "当前输出" : "选择此输出"}
      aria-pressed={active}
      disabled={active || loading}
      onClick={onSelect}
      style={{ aspectRatio: historyOutputAspectRatio(output) }}
      className="relative min-h-11 w-full overflow-hidden rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--surface-media)] disabled:cursor-default"
    >
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element -- API-backed execution output.
        <img
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          className="h-full w-full object-contain"
        />
      ) : (
        <span className="grid h-full place-items-center text-[var(--fg-2)]">
          <Icon className="h-5 w-5" />
        </span>
      )}
      {active ? (
        <span className="absolute right-1 top-1 grid h-5 w-5 place-items-center rounded-full bg-[var(--success)] text-[var(--success-on)]">
          <Check className="h-3 w-3" />
        </span>
      ) : loading ? (
        <span className="absolute inset-0 grid place-items-center bg-[var(--surface-scrim)] text-[var(--media-control-fg)]">
          <Loader2 className="h-4 w-4 animate-spin" />
        </span>
      ) : null}
    </button>
  );
}

function historyOutputPreviewSource(output: CanvasOutput): string | null {
  if (output.type === "video") {
    return (
      output.poster_url ??
      (output.video_id ? videoPosterUrl(output.video_id) : null) ??
      output.preview_url ??
      null
    );
  }
  return (
    output.preview_url ??
    (output.image_id
      ? imageVariantUrl(output.image_id, "thumb256")
      : null) ??
    output.url ??
    null
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

function InlineConfigConfirmation({
  removedConnections,
  onCancel,
  onConfirm,
}: {
  removedConnections: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <section
      aria-label="模式切换确认"
      className="m-4 grid gap-3 rounded-[var(--radius-card)] border border-[var(--danger)]/35 bg-[var(--danger-soft)] p-3"
    >
      <div role="alert" aria-live="assertive">
        <h3 className="type-body-sm font-medium text-[var(--danger-fg)]">
          确认切换模式
        </h3>
        <p className="mt-1 type-caption text-[var(--fg-1)]">
          继续后会移除 {removedConnections} 条不兼容连接。
        </p>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Button size="sm" variant="outline" onClick={onCancel}>
          取消
        </Button>
        <Button size="sm" variant="danger" onClick={onConfirm}>
          继续
        </Button>
      </div>
    </section>
  );
}

function ColorSwatchField({
  value,
  disabled,
  onChange,
}: {
  value: string | null;
  disabled?: boolean;
  onChange: (value: string | null) => void;
}) {
  return (
    <fieldset disabled={disabled} className="grid gap-2">
      <legend className="type-caption font-medium text-[var(--fg-1)]">
        颜色标记
      </legend>
      <div className="flex flex-wrap gap-2">
        {NODE_COLOR_OPTIONS.map((option) => {
          const selected = value === option.value;
          return (
            <button
              key={option.label}
              type="button"
              title={option.label}
              aria-label={option.label}
              aria-pressed={selected}
              onClick={() => onChange(option.value)}
              style={{ backgroundColor: option.color }}
              className={`relative grid h-11 w-11 place-items-center rounded-full border transition-[border-color,box-shadow,opacity] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-50 ${
                selected
                  ? "border-[var(--fg-0)] ring-2 ring-[var(--accent)]/35"
                  : "border-[var(--border-strong)]"
              }`}
            >
              {option.value === null ? (
                <span
                  aria-hidden
                  className="h-px w-6 rotate-45 bg-[var(--fg-2)]"
                />
              ) : null}
              {selected ? (
                <Check
                  className="absolute bottom-0.5 right-0.5 h-3.5 w-3.5 rounded-full bg-[var(--bg-0)] p-0.5 text-[var(--fg-0)]"
                  aria-hidden
                />
              ) : null}
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}

function InspectorShell({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--bg-1)] text-[var(--fg-0)]">
      <header className="shrink-0 border-b border-[var(--border)] px-4 py-3">
        <p className="type-page-kicker">{eyebrow}</p>
        <h2 className="type-card-title mt-1 truncate">{title}</h2>
      </header>
      {children}
    </div>
  );
}

function InspectorSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="grid gap-3 border-b border-[var(--border)] p-4 last:border-0">
      <h3 className="type-overline text-[var(--fg-2)]">{title}</h3>
      {children}
    </section>
  );
}

function SelectField({
  label,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  value: string;
  options: readonly SelectOption[];
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-1">
      <span className="type-caption font-medium text-[var(--fg-1)]">{label}</span>
      <select
        className={SELECT_CLASS}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.value)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function RangeField({
  label,
  value,
  min,
  max,
  suffix,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  suffix?: string;
  onChange: (value: number) => void;
}) {
  return (
    <label className="grid gap-2">
      <span className="flex items-center justify-between type-caption font-medium text-[var(--fg-1)]">
        {label}
        <span className="font-mono text-[var(--fg-0)]">
          {value}
          {suffix}
        </span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        value={value}
        onChange={(event) => onChange(Number(event.currentTarget.value))}
        className="h-11 w-full cursor-pointer accent-[var(--accent)]"
      />
    </label>
  );
}

function ToggleField({
  label,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex min-h-11 items-center justify-between gap-3">
      <span className="type-body-sm text-[var(--fg-1)]">{label}</span>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.checked)}
        className="h-5 w-5 accent-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-50"
      />
    </label>
  );
}

function ReadOnlyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 type-body-sm">
      <span className="text-[var(--fg-2)]">{label}</span>
      <span className="truncate text-[var(--fg-0)]">{value}</span>
    </div>
  );
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
