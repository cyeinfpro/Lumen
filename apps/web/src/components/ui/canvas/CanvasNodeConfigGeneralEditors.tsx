"use client";

import { MAX_PROMPT_CHARS } from "@/lib/promptLimits";
import { resolveCanvasTextOutput } from "@/lib/canvas/graph";
import type { CanvasNodeDefinition } from "@/lib/canvas/types";
import {
  Metric,
  UploadField,
} from "./CanvasNodeConfigControls";
import {
  CommitInput,
  CommitTextarea,
  ConfigSection,
  SelectField,
  ToggleField,
} from "./CanvasNodeConfigFields";
import type { SelectOption } from "./CanvasNodeConfigFields";
import type { CanvasNodeConfigEditorProps } from "./CanvasNodeConfigEditorContracts";

const SEPARATOR_OPTIONS: readonly SelectOption[] = [
  { value: "blank-line", label: "空行" },
  { value: "newline", label: "换行" },
  { value: "comma", label: "逗号" },
  { value: "space", label: "空格" },
  { value: "custom", label: "自定义" },
];

const KNOWN_SEPARATORS: Record<string, string> = {
  "blank-line": "\n\n",
  newline: "\n",
  comma: ", ",
  space: " ",
};

export function PromptConfig({
  node,
  patch,
}: CanvasNodeConfigEditorProps) {
  return (
    <ConfigSection title="提示词">
      <CommitTextarea
        label="内容"
        value={String(node.config.text ?? "")}
        maxLength={MAX_PROMPT_CHARS}
        rows={6}
        placeholder="描述主体、环境、构图、光线、风格和限制条件"
        onCommit={(text) => patch({ text })}
      />
      <ToggleField
        label="锁定文本"
        checked={node.config.locked === true}
        onChange={(locked) => patch({ locked })}
      />
    </ConfigSection>
  );
}

export function PromptMergeConfig({
  node,
  graph,
  patch,
}: CanvasNodeConfigEditorProps) {
  const separator = String(node.config.separator ?? "\n\n");
  const separatorMode =
    Object.entries(KNOWN_SEPARATORS).find(
      ([, value]) => value === separator,
    )?.[0] ?? "custom";
  const resolved = resolveCanvasTextOutput(graph, node.id) ?? "";
  const inputCount = graph.edges.filter(
    (edge) =>
      edge.target_node_id === node.id && edge.target_handle === "texts",
  ).length;
  return (
    <>
      <ConfigSection title="合并规则">
        <SelectField
          label="分隔方式"
          value={separatorMode}
          options={SEPARATOR_OPTIONS}
          onChange={(value) => {
            if (value !== "custom") {
              patch({ separator: KNOWN_SEPARATORS[value] ?? "\n\n" });
            } else if (separatorMode !== "custom") {
              patch({ separator: " / " });
            }
          }}
        />
        {separatorMode === "custom" ? (
          <CommitInput
            label="自定义分隔符"
            value={separator}
            maxLength={32}
            onCommit={(value) => patch({ separator: value })}
          />
        ) : null}
        <ToggleField
          label="清理首尾空白"
          checked={node.config.trim !== false}
          onChange={(trim) => patch({ trim })}
        />
        <ToggleField
          label="移除重复文本"
          checked={node.config.dedupe === true}
          onChange={(dedupe) => patch({ dedupe })}
        />
      </ConfigSection>
      <ConfigSection title="包裹文本">
        <CommitTextarea
          label="前缀"
          value={String(node.config.prefix ?? "")}
          maxLength={2_000}
          rows={2}
          onCommit={(prefix) => patch({ prefix })}
        />
        <CommitTextarea
          label="后缀"
          value={String(node.config.suffix ?? "")}
          maxLength={2_000}
          rows={2}
          onCommit={(suffix) => patch({ suffix })}
        />
      </ConfigSection>
      <ConfigSection title="输出">
        <div className="grid grid-cols-2 gap-2">
          <Metric label="输入" value={`${inputCount} 路`} />
          <Metric label="字符" value={resolved.length.toLocaleString()} />
        </div>
        <p className="max-h-28 overflow-y-auto whitespace-pre-wrap rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] p-3 type-caption leading-5 text-[var(--fg-1)]">
          {resolved || "暂无组合文本"}
        </p>
      </ConfigSection>
    </>
  );
}

export function VideoAssetConfig({
  node,
  patch,
  uploading,
  onUploadVideo,
}: CanvasNodeConfigEditorProps) {
  return (
    <ConfigSection title="视频素材">
      <CommitInput
        label="显示名称"
        value={String(node.config.display_name ?? "")}
        maxLength={255}
        onCommit={(displayName) =>
          patch({ display_name: displayName || null })
        }
      />
      <CommitInput
        label="视频 ID"
        value={String(node.config.video_id ?? "")}
        maxLength={36}
        onCommit={(videoId) => patch({ video_id: videoId })}
      />
      <UploadField
        accept="video/mp4,video/quicktime"
        busy={uploading}
        label="上传视频"
        onSelect={onUploadVideo}
      />
    </ConfigSection>
  );
}

export function FrameConfig({
  node,
  patch,
}: CanvasNodeConfigEditorProps) {
  return (
    <ConfigSection title="画框">
      <CommitInput
        label="运行标签"
        value={String(node.config.label ?? node.title)}
        maxLength={255}
        onCommit={(label) => patch({ label: label || "新画框" })}
      />
      <ToggleField
        label="运行视图隐藏"
        checked={node.config.hidden_in_run === true}
        onChange={(hiddenInRun) =>
          patch({ hidden_in_run: hiddenInRun })
        }
      />
      <ToggleField
        label="允许作为运行范围"
        checked={node.config.runnable_scope !== false}
        onChange={(runnableScope) =>
          patch({ runnable_scope: runnableScope })
        }
      />
    </ConfigSection>
  );
}

export function DeliveryConfig({
  node,
  graph,
  patch,
}: CanvasNodeConfigEditorProps) {
  const imageSources = Array.from(
    new Set(
      graph.edges
        .filter(
          (edge) =>
            edge.target_node_id === node.id &&
            edge.target_handle === "images",
        )
        .map((edge) => edge.source_node_id),
    ),
  )
    .map((nodeId) => graph.nodes.find((candidate) => candidate.id === nodeId))
    .filter((candidate): candidate is CanvasNodeDefinition => Boolean(candidate));
  const currentSource = String(
    node.config.thumbnail_source_node_id ?? "",
  );
  const sourceOptions: SelectOption[] = [
    { value: "", label: "自动选择首张图片" },
    ...imageSources.map((source) => ({
      value: source.id,
      label: source.title,
    })),
  ];
  if (
    currentSource &&
    !sourceOptions.some((option) => option.value === currentSource)
  ) {
    sourceOptions.push({
      value: currentSource,
      label: `${currentSource}（连接已移除）`,
    });
  }
  return (
    <ConfigSection title="交付">
      <ToggleField
        label="设为画布封面"
        checked={node.config.set_as_thumbnail !== false}
        onChange={(setAsThumbnail) =>
          patch({ set_as_thumbnail: setAsThumbnail })
        }
      />
      <SelectField
        label="封面来源"
        value={currentSource}
        options={sourceOptions}
        disabled={
          node.config.set_as_thumbnail === false || imageSources.length === 0
        }
        onChange={(value) =>
          patch({ thumbnail_source_node_id: value || null })
        }
      />
    </ConfigSection>
  );
}
