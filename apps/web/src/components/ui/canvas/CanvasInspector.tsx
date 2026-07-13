"use client";

import {
  Check,
  Image as ImageIcon,
  Loader2,
  Play,
  Trash2,
  Upload,
  Video,
} from "lucide-react";
import { useMemo, useState } from "react";

import { uploadImage } from "@/lib/apiClient";
import { imageVariantUrl, videoPosterUrl } from "@/lib/apiClient";
import type {
  CanvasDocument,
  CanvasNodeDefinition,
  CanvasNodeExecution,
  CanvasNodeType,
  CanvasOutput,
} from "@/lib/canvas/types";
import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import { useSelectCanvasOutputMutation } from "@/lib/queries/canvases";
import { Button, Input, toast } from "@/components/ui/primitives";
import { useCanvasStore } from "./CanvasStoreProvider";

const SELECT_CLASS =
  "h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 type-body-sm text-[var(--fg-0)] focus:border-[var(--accent)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-soft)] max-sm:min-h-11 max-sm:text-base";

export function CanvasInspector({
  document,
  onRunNode,
  runningNodeId,
}: {
  document: CanvasDocument;
  onRunNode: (nodeId: string) => void;
  runningNodeId?: string | null;
}) {
  const graph = useCanvasStore((state) => state.graph);
  const selectedNodeId = useCanvasStore((state) => state.selectedNodeId);
  const selectedEdgeId = useCanvasStore((state) => state.selectedEdgeId);
  const updateNodeConfig = useCanvasStore((state) => state.updateNodeConfig);
  const updateNodeTitle = useCanvasStore((state) => state.updateNodeTitle);
  const removeNodes = useCanvasStore((state) => state.removeNodes);
  const removeEdges = useCanvasStore((state) => state.removeEdges);
  const updateEdgeBinding = useCanvasStore(
    (state) => state.updateEdgeBinding,
  );
  const [uploading, setUploading] = useState(false);

  const node = graph.nodes.find((item) => item.id === selectedNodeId) ?? null;
  const edge = graph.edges.find((item) => item.id === selectedEdgeId) ?? null;
  const executions = useMemo(
    () =>
      document.recent_executions.filter(
        (execution) => execution.node_id === selectedNodeId,
      ),
    [document.recent_executions, selectedNodeId],
  );

  if (edge) {
    const source = graph.nodes.find((item) => item.id === edge.source_node_id);
    const target = graph.nodes.find((item) => item.id === edge.target_node_id);
    const sourceSelection = document.selections.find(
      (selection) =>
        selection.node_id === edge.source_node_id &&
        selection.execution_id !== null,
    );
    return (
      <InspectorShell eyebrow="连接" title={`${source?.title ?? "来源"} → ${target?.title ?? "目标"}`}>
        <InspectorSection title="输入绑定">
          <ReadOnlyRow label="类型" value={edge.data_type} />
          <ReadOnlyRow label="目标端口" value={edge.target_handle} />
          <div className="grid grid-cols-2 gap-2" role="group" aria-label="输入版本">
            <Button
              size="sm"
              variant={
                edge.binding_mode === "follow_active" ? "primary" : "outline"
              }
              onClick={() => updateEdgeBinding(edge.id, "follow_active")}
            >
              跟随当前
            </Button>
            <Button
              size="sm"
              variant={edge.binding_mode === "pinned" ? "primary" : "outline"}
              disabled={!sourceSelection}
              onClick={() => {
                if (!sourceSelection?.execution_id) return;
                updateEdgeBinding(
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
            onClick={() => removeEdges([edge.id])}
          >
            删除连接
          </Button>
        </div>
      </InspectorShell>
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
    if (
      removedConnections > 0 &&
      !window.confirm(
        `切换模式会移除 ${removedConnections} 条不兼容连接，是否继续？`,
      )
    ) {
      return;
    }
    updateNodeConfig(node.id, nextConfig);
  };
  const canRun = node.type === "image_generate" || node.type === "video_generate";

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
        </InspectorSection>

        <NodeConfigEditor
          node={node}
          patch={patch}
          uploading={uploading}
          onUpload={async (file) => {
            setUploading(true);
            try {
              const image = await uploadImage(file);
              patch({
                image_id: image.id,
                display_name: file.name,
              });
              toast.success("图片已上传");
            } catch (error) {
              toast.error(error instanceof Error ? error.message : "上传失败");
            } finally {
              setUploading(false);
            }
          }}
        />

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
  return (
    <InspectorSection title="生成参数">
      <SelectField
        label="比例"
        value={String(node.config.aspect_ratio ?? "1:1")}
        options={["1:1", "4:5", "3:2", "16:9", "9:16"]}
        onChange={(value) => patch({ aspect_ratio: value })}
      />
      <SelectField
        label="尺寸"
        value={String(node.config.quality ?? "2k")}
        options={["1k", "2k", "4k"]}
        onChange={(value) => patch({ quality: value })}
      />
      <SelectField
        label="渲染质量"
        value={String(node.config.render_quality ?? "high")}
        options={["auto", "low", "medium", "high"]}
        onChange={(value) => patch({ render_quality: value })}
      />
      <RangeField
        label="数量"
        value={Number(node.config.count ?? 1)}
        min={1}
        max={4}
        onChange={(value) => patch({ count: value })}
      />
      <ToggleField
        label="快速模式"
        checked={node.config.fast !== false}
        onChange={(checked) => patch({ fast: checked })}
      />
    </InspectorSection>
  );
}

function VideoGenerateConfig({ node, patch }: NodeConfigEditorProps) {
  return (
    <InspectorSection title="生成参数">
      <SelectField
        label="模式"
        value={String(node.config.mode ?? "t2v")}
        options={["t2v", "i2v", "reference"]}
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
        options={["480p", "720p", "1080p"]}
        onChange={(value) => patch({ resolution: value })}
      />
      <SelectField
        label="比例"
        value={String(node.config.aspect_ratio ?? "16:9")}
        options={["16:9", "9:16", "1:1", "4:3", "3:4"]}
        onChange={(value) => patch({ aspect_ratio: value })}
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
              <span className="type-mono-meta text-[var(--fg-2)]">{execution.status}</span>
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
  const src =
    output.preview_url ??
    output.url ??
    (output.type === "image" && output.image_id
      ? imageVariantUrl(output.image_id, "thumb256")
      : output.video_id
        ? output.poster_url ?? videoPosterUrl(output.video_id)
        : null);
  return (
    <button
      type="button"
      aria-label={active ? "当前输出" : "选择此输出"}
      aria-pressed={active}
      disabled={active || loading}
      onClick={onSelect}
      className="relative aspect-square overflow-hidden rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--surface-media)] disabled:cursor-default"
    >
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element -- API-backed execution output.
        <img src={src} alt="" className="h-full w-full object-cover" />
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
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-1">
      <span className="type-caption font-medium text-[var(--fg-1)]">{label}</span>
      <select
        className={SELECT_CLASS}
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
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
        className="accent-[var(--accent)]"
      />
    </label>
  );
}

function ToggleField({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex min-h-11 items-center justify-between gap-3">
      <span className="type-body-sm text-[var(--fg-1)]">{label}</span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.currentTarget.checked)}
        className="h-5 w-5 accent-[var(--accent)]"
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
