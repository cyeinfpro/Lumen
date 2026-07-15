"use client";

import {
  CheckCircle2,
  ChevronDown,
  CircleAlert,
} from "lucide-react";
import {
  useRef,
  useState,
  type ComponentType,
} from "react";

import {
  billingModelForAction,
  durationOptionsForModel,
  estimateHoldMicro,
  firstModelForAction,
  preferredDuration,
  preferredResolution,
  resolutionOptionsForModel,
  videoModelsForAction,
  videoUnavailableReasonMessage,
} from "@/app/video/video-options-model";
import { Button, Input, Textarea } from "@/components/ui/primitives";
import {
  canvasVideoReferenceCounts,
  resolveCanvasTextOutput,
  validateCanvasNodeExecution,
} from "@/lib/canvas/graph";
import {
  CANVAS_NODE_SPECS,
  canvasFixedVideoMode,
  canvasVideoModeForNode,
  isCanvasExecutableNodeType,
} from "@/lib/canvas/registry";
import type {
  CanvasGraph,
  CanvasNodeDefinition,
  CanvasNodeType,
} from "@/lib/canvas/types";
import { formatRmb } from "@/lib/money";
import { MAX_PROMPT_CHARS } from "@/lib/promptLimits";
import type {
  VideoAction,
  VideoOptionsOut,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  FixedSizeInput,
  Metric,
  OptionalSeedInput,
  ReadOnlyValue,
  UploadField,
  normalizedCrop,
  selectOptionsWithCurrent,
  uniqueStrings,
  videoModeLabel,
} from "./CanvasNodeConfigControls";

const SELECT_CLASS =
  "h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 type-body-sm text-[var(--fg-0)] focus:border-[var(--accent)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-soft)] max-sm:min-h-11 max-sm:text-base";

type SelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
};

export interface CanvasNodeConfigEditorProps {
  node: CanvasNodeDefinition;
  graph: CanvasGraph;
  patch: (next: Record<string, unknown>) => void;
  uploading: boolean;
  onUploadImage: (file: File) => Promise<void>;
  onUploadVideo: (file: File) => Promise<void>;
  videoOptions?: VideoOptionsOut;
  videoOptionsLoading?: boolean;
  videoOptionsError?: string | null;
  videoOptionsRetrying?: boolean;
  onRetryVideoOptions?: () => void;
}

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
  { value: "standard", label: "标准（旧配置）" },
  { value: "high", label: "高质量（旧配置）" },
];

const RENDER_QUALITY_OPTIONS: readonly SelectOption[] = [
  { value: "auto", label: "自动" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

const SIZE_MODE_OPTIONS: readonly SelectOption[] = [
  { value: "auto", label: "按比例自动计算" },
  { value: "fixed", label: "固定像素尺寸" },
];

const IMAGE_FORMAT_OPTIONS: readonly SelectOption[] = [
  { value: "webp", label: "WebP" },
  { value: "jpeg", label: "JPEG" },
  { value: "png", label: "PNG" },
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

export function CanvasNodeConfigEditor(
  props: CanvasNodeConfigEditorProps,
) {
  const Editor = CONFIG_EDITORS[props.node.type];
  return (
    <>
      <InputStatusSection node={props.node} graph={props.graph} />
      <Editor {...props} />
    </>
  );
}

function InputStatusSection({
  node,
  graph,
}: Pick<CanvasNodeConfigEditorProps, "node" | "graph">) {
  const ports = CANVAS_NODE_SPECS[node.type].inputs;
  if (ports.length === 0) return null;
  const counts = new Map<string, number>();
  for (const edge of graph.edges) {
    if (edge.target_node_id !== node.id) continue;
    counts.set(edge.target_handle, (counts.get(edge.target_handle) ?? 0) + 1);
  }
  const executionValidation = isCanvasExecutableNodeType(node.type)
    ? validateCanvasNodeExecution(graph, node.id)
    : null;
  const executionIssue =
    executionValidation && !executionValidation.valid
      ? executionValidation.reason
      : null;
  return (
    <ConfigSection title="输入">
      <div className="grid gap-2">
        {ports.map((port) => {
          const count = counts.get(port.id) ?? 0;
          const missing = port.required === true && count === 0;
          const connected = count > 0;
          return (
            <div
              key={port.id}
              className="flex min-h-9 items-center justify-between gap-3"
            >
              <span className="min-w-0 truncate type-body-sm text-[var(--fg-1)]">
                {port.label}
              </span>
              <span
                className={cn(
                  "inline-flex shrink-0 items-center gap-1.5 type-caption tabular-nums",
                  missing
                    ? "text-[var(--danger-fg)]"
                    : connected
                      ? "text-[var(--success-fg)]"
                      : "text-[var(--fg-3)]",
                )}
              >
                {missing ? (
                  <CircleAlert className="h-3.5 w-3.5" aria-hidden />
                ) : connected ? (
                  <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
                ) : null}
                {connected ? `${count} 个` : "未连接"}
                {port.required ? " · 必需" : ""}
              </span>
            </div>
          );
        })}
      </div>
      {executionIssue ? (
        <p role="alert" className="type-caption text-[var(--danger-fg)]">
          {executionIssue}
        </p>
      ) : null}
    </ConfigSection>
  );
}

function PromptConfig({ node, patch }: CanvasNodeConfigEditorProps) {
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

function PromptMergeConfig({
  node,
  graph,
  patch,
}: CanvasNodeConfigEditorProps) {
  const separator = String(node.config.separator ?? "\n\n");
  const separatorMode =
    Object.entries(KNOWN_SEPARATORS).find(([, value]) => value === separator)?.[0] ??
    "custom";
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

function ImageAssetConfig({
  node,
  patch,
  uploading,
  onUploadImage,
}: CanvasNodeConfigEditorProps) {
  const isMask = node.type === "mask_asset";
  const crop = normalizedCrop(node.config.crop);
  return (
    <>
      <ConfigSection title={isMask ? "遮罩素材" : "图片素材"}>
        <CommitInput
          label="显示名称"
          value={String(node.config.display_name ?? "")}
          maxLength={255}
          onCommit={(displayName) =>
            patch({ display_name: displayName || null })
          }
        />
        <CommitInput
          label="图片 ID"
          value={String(node.config.image_id ?? "")}
          maxLength={36}
          onCommit={(imageId) => patch({ image_id: imageId })}
        />
        <UploadField
          accept={isMask ? "image/png" : "image/png,image/jpeg,image/webp"}
          busy={uploading}
          label={isMask ? "上传遮罩" : "上传图片"}
          onSelect={onUploadImage}
        />
      </ConfigSection>
      <ConfigSection title="预览裁切">
        <ToggleField
          label="启用裁切"
          checked={crop !== null}
          onChange={(enabled) =>
            patch({
              crop: enabled
                ? { x: 0, y: 0, width: 1, height: 1 }
                : null,
            })
          }
        />
        {crop ? (
          <div className="grid gap-3">
            <RangeField
              label="水平起点"
              value={Math.round(crop.x * 100)}
              min={0}
              max={Math.round((1 - crop.width) * 100)}
              suffix="%"
              onChange={(value) =>
                patch({
                  crop: { ...crop, x: value / 100 },
                })
              }
            />
            <RangeField
              label="垂直起点"
              value={Math.round(crop.y * 100)}
              min={0}
              max={Math.round((1 - crop.height) * 100)}
              suffix="%"
              onChange={(value) =>
                patch({
                  crop: { ...crop, y: value / 100 },
                })
              }
            />
            <RangeField
              label="裁切宽度"
              value={Math.round(crop.width * 100)}
              min={5}
              max={Math.round((1 - crop.x) * 100)}
              suffix="%"
              onChange={(value) =>
                patch({
                  crop: { ...crop, width: value / 100 },
                })
              }
            />
            <RangeField
              label="裁切高度"
              value={Math.round(crop.height * 100)}
              min={5}
              max={Math.round((1 - crop.y) * 100)}
              suffix="%"
              onChange={(value) =>
                patch({
                  crop: { ...crop, height: value / 100 },
                })
              }
            />
          </div>
        ) : null}
      </ConfigSection>
    </>
  );
}

function VideoAssetConfig({
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

function ImageGenerateConfig(props: CanvasNodeConfigEditorProps) {
  return (
    <>
      <ImageGenerationParameters {...props} />
      <ImageOutputParameters {...props} />
    </>
  );
}

function ImageGenerationParameters({
  node,
  patch,
}: CanvasNodeConfigEditorProps) {
  const sizeMode = String(node.config.size_mode ?? "auto");
  const aspectRatio = String(node.config.aspect_ratio ?? "1:1");
  return (
    <ConfigSection title={imageParameterSectionTitle(node.type)}>
      <SelectField
        label="比例"
        value={aspectRatio}
        options={selectOptionsWithCurrent(
          IMAGE_ASPECT_OPTIONS.map((option) => option.value),
          aspectRatio,
          IMAGE_ASPECT_OPTIONS,
          true,
        )}
        onChange={(value) => patch({ aspect_ratio: value })}
      />
      <SelectField
        label="输出尺寸"
        value={String(node.config.quality ?? "2k").toLowerCase()}
        options={IMAGE_QUALITY_OPTIONS}
        onChange={(quality) =>
          patch({
            quality,
            size: imageSizeForQuality(quality, node.config.size),
          })
        }
      />
      <SelectField
        label="尺寸模式"
        value={sizeMode}
        options={SIZE_MODE_OPTIONS}
        onChange={(value) =>
          patch({
            size_mode: value,
            fixed_size: value === "fixed" ? node.config.fixed_size ?? "" : null,
          })
        }
      />
      {sizeMode === "fixed" ? (
        <FixedSizeInput
          value={String(node.config.fixed_size ?? "")}
          onCommit={(fixedSize) => patch({ fixed_size: fixedSize || null })}
        />
      ) : null}
      <SelectField
        label="渲染质量"
        value={String(node.config.render_quality ?? "high")}
        options={RENDER_QUALITY_OPTIONS}
        onChange={(value) => patch({ render_quality: value })}
      />
      <RangeField
        label="候选数量"
        value={Number(node.config.count ?? 1)}
        min={1}
        max={10}
        onChange={(value) => patch({ count: value })}
      />
      <ToggleField
        label="快速模式"
        checked={node.config.fast !== false}
        onChange={(fast) => patch({ fast })}
      />
    </ConfigSection>
  );
}

function ImageOutputParameters({
  node,
  patch,
}: CanvasNodeConfigEditorProps) {
  const outputFormat = String(node.config.output_format ?? "webp");
  const compression = numericConfigValue(node.config.output_compression);
  return (
    <ConfigSection title="输出">
      <SelectField
        label="图片格式"
        value={outputFormat}
        options={IMAGE_FORMAT_OPTIONS}
        onChange={(value) => patch(imageFormatPatch(value))}
      />
      <SelectField
        label="背景"
        value={String(node.config.background ?? "auto")}
        options={IMAGE_BACKGROUND_OPTIONS}
        onChange={(value) => patch(imageBackgroundPatch(value))}
      />
      {outputFormat === "png" ? null : (
        <ImageCompressionControls compression={compression} patch={patch} />
      )}
      <SelectField
        label="内容审核"
        value={String(node.config.moderation ?? "low")}
        options={IMAGE_MODERATION_OPTIONS}
        onChange={(value) => patch({ moderation: value })}
      />
    </ConfigSection>
  );
}

function ImageCompressionControls({
  compression,
  patch,
}: {
  compression: number | null;
  patch: CanvasNodeConfigEditorProps["patch"];
}) {
  return (
    <>
      <ToggleField
        label="自定义压缩"
        checked={compression !== null}
        onChange={(enabled) =>
          patch({ output_compression: enabled ? 90 : null })
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
  );
}

function imageParameterSectionTitle(type: CanvasNodeType): string {
  if (type === "image_edit") return "编辑参数";
  if (type === "image_inpaint") return "重绘参数";
  if (type === "image_upscale") return "高清参数";
  return "生成参数";
}

function imageSizeForQuality(quality: string, current: unknown): string {
  const normalizedQuality = quality.toLowerCase();
  if (["1k", "2k", "4k"].includes(normalizedQuality)) {
    return normalizedQuality.toUpperCase();
  }
  const normalizedCurrent = String(current ?? "1K").toUpperCase();
  return ["1K", "2K", "4K"].includes(normalizedCurrent)
    ? normalizedCurrent
    : "1K";
}

function numericConfigValue(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function imageFormatPatch(value: string): Record<string, unknown> {
  if (value === "png") {
    return { output_format: value, output_compression: null };
  }
  return { output_format: value };
}

function imageBackgroundPatch(value: string): Record<string, unknown> {
  if (value === "transparent") {
    return {
      background: value,
      output_format: "png",
      output_compression: null,
    };
  }
  return { background: value };
}

function VideoGenerateConfig(props: CanvasNodeConfigEditorProps) {
  const model = buildVideoEditorModel(props);
  return (
    <>
      <VideoParameterSection props={props} model={model} />
      <VideoEstimateSection estimate={model.estimate} />
      <VideoAdvancedParameters node={props.node} patch={props.patch} />
    </>
  );
}

interface VideoEditorModel {
  action: VideoAction;
  fixedMode: ReturnType<typeof canvasFixedVideoMode>;
  compatibleModels: VideoOptionsOut["models"];
  configuredModel: string;
  effectiveModel: string;
  currentResolution: string;
  availableResolutions: string[];
  effectiveResolution: string;
  currentDuration: number;
  availableDurations: number[];
  effectiveDuration: number;
  aspectOptions: SelectOption[];
  capabilityIssue: string | null;
  estimate: ReturnType<typeof estimateHoldMicro>;
}

function buildVideoEditorModel(
  props: CanvasNodeConfigEditorProps,
): VideoEditorModel {
  const {
    node,
    graph,
    videoOptions,
  } = props;
  const action = (canvasVideoModeForNode(node) ?? "t2v") as VideoAction;
  const fixedMode = canvasFixedVideoMode(node.type);
  const referenceCounts = canvasVideoReferenceCounts(graph, node.id);
  const compatibleModels = videoModelsForAction(
    videoOptions,
    action,
    referenceCounts,
  );
  const configuredModel = String(node.config.model ?? "");
  const configuredModelAvailable =
    !configuredModel ||
    compatibleModels.some((item) => item.model === configuredModel);
  const effectiveModel =
    configuredModel && configuredModelAvailable
      ? configuredModel
      : firstModelForAction(videoOptions, action, referenceCounts);
  const currentResolution = String(node.config.resolution ?? "720p");
  const availableResolutions = resolutionOptionsForModel(
    videoOptions,
    effectiveModel,
  );
  const effectiveResolution = currentOrPreferredResolution(
    currentResolution,
    availableResolutions,
  );
  const currentDuration = Number(node.config.duration_s ?? 5);
  const availableDurations = durationOptionsForModel(
    videoOptions,
    effectiveModel,
    action,
    effectiveResolution,
  );
  const effectiveDuration = currentOrPreferredDuration(
    currentDuration,
    availableDurations,
  );
  const aspectOptions = videoAspectOptions(videoOptions, node);
  const capabilityIssue = videoCapabilityIssue({
    optionsLoaded: Boolean(videoOptions),
    optionsEnabled: videoOptions?.enabled !== false,
    unavailableReason: videoOptions?.unavailable_reason,
    compatibleModelCount: compatibleModels.length,
    configuredModel,
    configuredModelAvailable,
    currentResolution,
    availableResolutions,
    currentDuration,
    availableDurations,
    currentAspectRatio: String(node.config.aspect_ratio ?? "16:9"),
    availableAspectRatios: videoAspectValues(videoOptions),
  });
  const referenceHasVideo = referenceCounts.video > 0;
  const billingModel = billingModelForAction(
    videoOptions,
    effectiveModel,
    action,
  );
  const estimate = videoEstimate(videoOptions, {
    effectiveModel,
    billingModel,
    action,
    effectiveResolution,
    effectiveDuration,
    referenceHasVideo,
  });
  return {
    action,
    fixedMode,
    compatibleModels,
    configuredModel,
    effectiveModel,
    currentResolution,
    availableResolutions,
    effectiveResolution,
    currentDuration,
    availableDurations,
    effectiveDuration,
    aspectOptions,
    capabilityIssue,
    estimate,
  };
}

function VideoParameterSection({
  props,
  model,
}: {
  props: CanvasNodeConfigEditorProps;
  model: VideoEditorModel;
}) {
  const {
    node,
    patch,
    videoOptions,
    videoOptionsLoading,
    videoOptionsError,
    videoOptionsRetrying,
    onRetryVideoOptions,
  } = props;
  return (
    <ConfigSection title="视频参数">
      {model.fixedMode ? (
        <ReadOnlyValue
          label="模式"
          value={videoModeLabel(model.fixedMode)}
        />
      ) : (
        <SelectField
          label="模式"
          value={model.action}
          options={VIDEO_MODE_OPTIONS}
          onChange={(value) => patch({ mode: value, model: null })}
        />
      )}
      <SelectField
        label="模型"
        value={model.configuredModel}
        options={videoModelSelectOptions(model)}
        disabled={videoOptionsLoading}
        onChange={(value) =>
          patch(videoModelPatch(value, videoOptions, model))
        }
      />
      <SelectField
        label="分辨率"
        value={model.currentResolution}
        options={selectOptionsWithCurrent(
          model.availableResolutions,
          model.currentResolution,
          VIDEO_RESOLUTION_OPTIONS,
          Boolean(videoOptions),
        )}
        onChange={(resolution) =>
          patch(videoResolutionPatch(resolution, videoOptions, model))
        }
      />
      <SelectField
        label="时长"
        value={String(model.currentDuration)}
        options={videoDurationSelectOptions(
          model.availableDurations,
          model.currentDuration,
          Boolean(videoOptions),
        )}
        onChange={(value) => patch({ duration_s: Number(value) })}
      />
      <SelectField
        label="比例"
        value={String(node.config.aspect_ratio ?? "16:9")}
        options={model.aspectOptions}
        onChange={(value) => patch({ aspect_ratio: value })}
      />
      <ToggleField
        label="生成音频"
        checked={node.config.generate_audio === true}
        disabled={videoOptions?.generate_audio === false}
        onChange={(generateAudio) => patch({ generate_audio: generateAudio })}
      />
      {model.capabilityIssue ? (
        <p role="alert" className="type-caption text-[var(--danger-fg)]">
          {model.capabilityIssue}
        </p>
      ) : null}
      {videoOptionsError ? (
        <div className="grid gap-2">
          <p role="alert" className="type-caption text-[var(--danger-fg)]">
            {videoOptionsError}
          </p>
          {onRetryVideoOptions ? (
            <Button
              variant="secondary"
              loading={videoOptionsRetrying}
              onClick={onRetryVideoOptions}
            >
              重试加载
            </Button>
          ) : null}
        </div>
      ) : null}
    </ConfigSection>
  );
}

function VideoEstimateSection({
  estimate,
}: {
  estimate: VideoEditorModel["estimate"];
}) {
  return (
    <ConfigSection title="预计消耗">
      <div className="grid grid-cols-2 gap-2">
        <Metric
          label="预计预扣"
          value={estimate ? formatRmb(estimate.micro / 1_000_000) : "-"}
        />
        <Metric
          label="Token 上限"
          value={estimate ? estimate.tokens.toLocaleString() : "-"}
        />
      </div>
    </ConfigSection>
  );
}

function VideoAdvancedParameters({
  node,
  patch,
}: Pick<CanvasNodeConfigEditorProps, "node" | "patch">) {
  return (
    <details className="group border-b border-[var(--border)]">
      <summary className="flex min-h-12 cursor-pointer list-none items-center justify-between gap-3 px-4 type-overline text-[var(--fg-2)] hover:bg-[var(--bg-2)]">
        高级参数
        <ChevronDown
          className="h-4 w-4 transition-transform group-open:rotate-180"
          aria-hidden
        />
      </summary>
      <div className="grid gap-3 border-t border-[var(--border-subtle)] p-4">
        <OptionalSeedInput
          value={
            typeof node.config.seed === "number"
              ? node.config.seed
              : null
          }
          onCommit={(seed) => patch({ seed })}
        />
        <ToggleField
          label="添加水印"
          checked={node.config.watermark === true}
          onChange={(watermark) => patch({ watermark })}
        />
      </div>
    </details>
  );
}

function currentOrPreferredResolution(
  current: string,
  available: string[],
): string {
  return available.includes(current) ? current : preferredResolution(available);
}

function currentOrPreferredDuration(
  current: number,
  available: number[],
): number {
  return available.includes(current) ? current : preferredDuration(available);
}

function videoAspectOptions(
  options: VideoOptionsOut | undefined,
  node: CanvasNodeDefinition,
): SelectOption[] {
  const current = String(node.config.aspect_ratio ?? "16:9");
  return selectOptionsWithCurrent(
    videoAspectValues(options),
    current,
    VIDEO_ASPECT_OPTIONS,
    Boolean(options),
  );
}

function videoAspectValues(options: VideoOptionsOut | undefined): string[] {
  return options?.aspect_ratios?.length
    ? uniqueStrings(options.aspect_ratios)
    : VIDEO_ASPECT_OPTIONS.map((item) => item.value);
}

function videoCapabilityIssue(input: {
  optionsLoaded: boolean;
  optionsEnabled: boolean;
  unavailableReason?: string | null;
  compatibleModelCount: number;
  configuredModel: string;
  configuredModelAvailable: boolean;
  currentResolution: string;
  availableResolutions: string[];
  currentDuration: number;
  availableDurations: number[];
  currentAspectRatio: string;
  availableAspectRatios: string[];
}): string | null {
  if (!input.optionsLoaded) return null;
  if (!input.optionsEnabled) {
    return videoUnavailableReasonMessage(input.unavailableReason);
  }
  if (input.compatibleModelCount === 0) {
    return "当前模式没有可用的视频模型";
  }
  const issues: string[] = [];
  if (input.configuredModel && !input.configuredModelAvailable) {
    issues.push("模型");
  }
  if (!input.availableResolutions.includes(input.currentResolution)) {
    issues.push("分辨率");
  }
  if (!input.availableDurations.includes(input.currentDuration)) {
    issues.push("时长");
  }
  if (!input.availableAspectRatios.includes(input.currentAspectRatio)) {
    issues.push("比例");
  }
  return issues.length > 0
    ? `当前${issues.join("、")}不可用，请重新选择兼容参数`
    : null;
}

function videoEstimate(
  options: VideoOptionsOut | undefined,
  input: {
    effectiveModel: string;
    billingModel: string;
    action: VideoAction;
    effectiveResolution: string;
    effectiveDuration: number;
    referenceHasVideo: boolean;
  },
) {
  if (!input.effectiveModel) return null;
  return estimateHoldMicro(options, {
    model: input.effectiveModel,
    billingModel: input.billingModel,
    action: input.action,
    resolution: input.effectiveResolution,
    durationS: input.effectiveDuration,
    referenceHasVideo: input.referenceHasVideo,
  });
}

function videoModelSelectOptions(model: VideoEditorModel): SelectOption[] {
  const options: SelectOption[] = [
    { value: "", label: "系统自动选择" },
    ...model.compatibleModels.map((item) => ({
      value: item.model,
      label: item.model,
    })),
  ];
  const configuredIsListed = model.compatibleModels.some(
    (item) => item.model === model.configuredModel,
  );
  if (model.configuredModel && !configuredIsListed) {
    options.push({
      value: model.configuredModel,
      label: `${model.configuredModel}（当前不可用）`,
      disabled: true,
    });
  }
  return options;
}

function videoDurationSelectOptions(
  durations: number[],
  current: number,
  optionsLoaded: boolean,
): SelectOption[] {
  const options: SelectOption[] = durations.map((duration) => ({
    value: String(duration),
    label: duration === -1 ? "智能时长" : `${duration} 秒`,
  }));
  if (optionsLoaded && !durations.includes(current)) {
    options.unshift({
      value: String(current),
      label: `${current === -1 ? "智能时长" : `${current} 秒`}（当前不可用）`,
      disabled: true,
    });
  }
  return options;
}

function videoModelPatch(
  value: string,
  options: VideoOptionsOut | undefined,
  model: VideoEditorModel,
): Record<string, unknown> {
  const nextModel = value || model.compatibleModels[0]?.model || "";
  const resolutions = resolutionOptionsForModel(options, nextModel);
  const resolution = currentOrPreferredResolution(
    model.currentResolution,
    resolutions,
  );
  const durations = durationOptionsForModel(
    options,
    nextModel,
    model.action,
    resolution,
  );
  return {
    model: value || null,
    resolution,
    duration_s: currentOrPreferredDuration(model.currentDuration, durations),
  };
}

function videoResolutionPatch(
  resolution: string,
  options: VideoOptionsOut | undefined,
  model: VideoEditorModel,
): Record<string, unknown> {
  const durations = durationOptionsForModel(
    options,
    model.effectiveModel,
    model.action,
    resolution,
  );
  return {
    resolution,
    duration_s: currentOrPreferredDuration(model.currentDuration, durations),
  };
}

function NoteConfig({ node, patch }: CanvasNodeConfigEditorProps) {
  const tags = Array.isArray(node.config.tags)
    ? node.config.tags.filter((tag): tag is string => typeof tag === "string")
    : [];
  return (
    <ConfigSection title="备注">
      <CommitTextarea
        label="内容"
        value={String(node.config.text ?? "")}
        maxLength={20_000}
        rows={8}
        placeholder="记录创作说明、审核意见或交付要求"
        onCommit={(text) => patch({ text })}
      />
      <CommitInput
        label="标签"
        value={tags.join("，")}
        maxLength={395}
        placeholder="用逗号分隔，最多 12 个"
        onCommit={(raw) =>
          patch({
            tags: Array.from(
              new Set(
                raw
                  .split(/[,，]/)
                  .map((tag) => tag.trim())
                  .filter(Boolean)
                  .slice(0, 12),
              ),
            ),
          })
        }
      />
    </ConfigSection>
  );
}

function FrameConfig({ node, patch }: CanvasNodeConfigEditorProps) {
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

function DeliveryConfig({
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

const CONFIG_EDITORS: Record<
  CanvasNodeType,
  ComponentType<CanvasNodeConfigEditorProps>
> = {
  prompt: PromptConfig,
  prompt_merge: PromptMergeConfig,
  image_asset: ImageAssetConfig,
  mask_asset: ImageAssetConfig,
  video_asset: VideoAssetConfig,
  image_generate: ImageGenerateConfig,
  image_edit: ImageGenerateConfig,
  image_inpaint: ImageGenerateConfig,
  image_upscale: ImageGenerateConfig,
  video_generate: VideoGenerateConfig,
  video_text_generate: VideoGenerateConfig,
  video_image_generate: VideoGenerateConfig,
  video_reference_generate: VideoGenerateConfig,
  note: NoteConfig,
  frame: FrameConfig,
  delivery: DeliveryConfig,
};

function ConfigSection({
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
      <span className="type-caption font-medium text-[var(--fg-1)]">
        {label}
      </span>
      <select
        className={SELECT_CLASS}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.value)}
      >
        {options.map((option) => (
          <option
            key={`${option.value}:${option.label}`}
            value={option.value}
            disabled={option.disabled}
          >
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
  step = 1,
  suffix,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  suffix?: string;
  onChange: (value: number) => void;
}) {
  const boundedValue = Math.min(Math.max(value, min), Math.max(min, max));
  return (
    <RangeFieldControl
      key={`${boundedValue}:${min}:${max}:${step}`}
      label={label}
      value={boundedValue}
      min={min}
      max={max}
      step={step}
      suffix={suffix}
      onChange={onChange}
    />
  );
}

function RangeFieldControl({
  label,
  value,
  min,
  max,
  step,
  suffix,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  suffix?: string;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(value);
  const committedRef = useRef(value);
  const commit = () => {
    if (draft === committedRef.current) return;
    committedRef.current = draft;
    onChange(draft);
  };
  return (
    <label className="grid gap-2">
      <span className="flex items-center justify-between gap-3 type-caption font-medium text-[var(--fg-1)]">
        {label}
        <span className="font-mono text-[var(--fg-0)]">
          {draft}
          {suffix}
        </span>
      </span>
      <input
        type="range"
        min={min}
        max={Math.max(min, max)}
        step={step}
        value={draft}
        onChange={(event) => setDraft(Number(event.currentTarget.value))}
        onPointerUp={commit}
        onKeyUp={commit}
        onBlur={commit}
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
    <label className="flex min-h-11 cursor-pointer items-center justify-between gap-3">
      <span className="type-body-sm text-[var(--fg-1)]">{label}</span>
      <span className="relative inline-flex h-6 w-10 shrink-0">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.currentTarget.checked)}
          className="peer sr-only"
        />
        <span className="absolute inset-0 rounded-full border border-[var(--border-strong)] bg-[var(--bg-2)] transition-colors peer-checked:border-[var(--accent-border)] peer-checked:bg-[var(--accent)] peer-disabled:cursor-not-allowed peer-disabled:opacity-50" />
        <span className="pointer-events-none absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-[var(--fg-0)] shadow-[var(--shadow-1)] transition-transform peer-checked:translate-x-4" />
      </span>
    </label>
  );
}

function CommitInput({
  value,
  onCommit,
  ...props
}: Omit<React.ComponentProps<typeof Input>, "value" | "defaultValue" | "onChange"> & {
  value: string;
  onCommit: (value: string) => void;
}) {
  return (
    <Input
      key={value}
      {...props}
      defaultValue={value}
      onBlur={(event) => {
        const nextValue = event.currentTarget.value.trim();
        if (nextValue !== value) onCommit(nextValue);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          event.currentTarget.blur();
        }
        if (event.key === "Escape") {
          event.preventDefault();
          event.currentTarget.value = value;
          event.currentTarget.blur();
        }
      }}
    />
  );
}

function CommitTextarea({
  value,
  onCommit,
  ...props
}: Omit<
  React.ComponentProps<typeof Textarea>,
  "value" | "defaultValue" | "onChange"
> & {
  value: string;
  onCommit: (value: string) => void;
}) {
  return (
    <Textarea
      key={value}
      {...props}
      defaultValue={value}
      onBlur={(event) => {
        const nextValue = event.currentTarget.value;
        if (nextValue !== value) onCommit(nextValue);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.currentTarget.value = value;
          event.currentTarget.blur();
        }
      }}
    />
  );
}
