"use client";

import { ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import {
  billingModelForAction,
  durationOptionsForModel,
  estimateHoldMicro,
  preferredDuration,
  preferredResolution,
  resolutionOptionsForModel,
  videoModelsForAction,
  videoUnavailableReasonMessage,
} from "@/lib/video/optionsModel";
import { canvasVideoReferenceCounts } from "@/lib/canvas/graph";
import {
  canvasFixedVideoMode,
  canvasVideoModeForNode,
} from "@/lib/canvas/registry";
import type { CanvasNodeDefinition } from "@/lib/canvas/types";
import { formatRmb } from "@/lib/money";
import type {
  VideoAction,
  VideoOptionsOut,
} from "@/lib/types";
import {
  Metric,
  OptionalSeedInput,
  ReadOnlyValue,
  selectOptionsWithCurrent,
  uniqueStrings,
  videoModeLabel,
} from "./CanvasNodeConfigControls";
import {
  ConfigSection,
  SelectField,
  ToggleField,
} from "./CanvasNodeConfigFields";
import type { SelectOption } from "./CanvasNodeConfigFields";
import type { CanvasNodeConfigEditorProps } from "./CanvasNodeConfigEditorContracts";

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

export function VideoGenerateConfig(
  props: CanvasNodeConfigEditorProps,
) {
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
  capabilityModels: VideoOptionsOut["models"];
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
  const configuredModelOption = compatibleModels.find(
    (item) => item.model === configuredModel,
  );
  const configuredModelAvailable =
    !configuredModel || Boolean(configuredModelOption);
  const capabilityModels = configuredModelOption
    ? [configuredModelOption]
    : compatibleModels;
  const currentResolution = String(node.config.resolution ?? "720p");
  const availableResolutions = videoResolutionOptionsForModels(
    videoOptions,
    capabilityModels,
  );
  const effectiveResolution = currentOrPreferredResolution(
    currentResolution,
    availableResolutions,
  );
  const currentDuration = Number(node.config.duration_s ?? 5);
  const availableDurations = videoDurationOptionsForModels(
    videoOptions,
    capabilityModels,
    action,
    effectiveResolution,
  );
  const effectiveDuration = currentOrPreferredDuration(
    currentDuration,
    availableDurations,
  );
  const effectiveModel =
    selectVideoModelForParameters(
      capabilityModels,
      videoOptions,
      action,
      effectiveResolution,
      effectiveDuration,
    )?.model ?? "";
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
    capabilityModels,
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

export function videoResolutionOptionsForModels(
  options: VideoOptionsOut | undefined,
  models: VideoOptionsOut["models"],
): string[] {
  if (models.length === 0) return resolutionOptionsForModel(options, "");
  return uniqueStrings(
    models.flatMap((item) =>
      resolutionOptionsForModel(options, item.model),
    ),
  );
}

export function videoDurationOptionsForModels(
  options: VideoOptionsOut | undefined,
  models: VideoOptionsOut["models"],
  action: VideoAction,
  resolution: string,
): number[] {
  const candidates = models.length
    ? models.filter((item) =>
        resolutionOptionsForModel(options, item.model).includes(resolution),
      )
    : [];
  const fallbackCandidates =
    candidates.length > 0
      ? candidates
      : models.length > 0
        ? models
        : [{ model: "" } as VideoOptionsOut["models"][number]];
  return uniqueNumbers(
    fallbackCandidates.flatMap((item) =>
      durationOptionsForModel(options, item.model, action, resolution),
    ),
  );
}

export function selectVideoModelForParameters(
  models: VideoOptionsOut["models"],
  options: VideoOptionsOut | undefined,
  action: VideoAction,
  resolution: string,
  duration: number,
): VideoOptionsOut["models"][number] | undefined {
  return models.find(
    (item) =>
      resolutionOptionsForModel(options, item.model).includes(resolution) &&
      durationOptionsForModel(
        options,
        item.model,
        action,
        resolution,
      ).includes(duration),
  );
}

function uniqueNumbers(values: number[]): number[] {
  return Array.from(new Set(values.filter(Number.isFinite)));
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
    ? `当前${issues.join("、")}不可用，重新选择兼容参数`
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
  const nextModels = value
    ? model.compatibleModels.filter((item) => item.model === value)
    : model.compatibleModels;
  const resolutions = videoResolutionOptionsForModels(options, nextModels);
  const resolution = currentOrPreferredResolution(
    model.currentResolution,
    resolutions,
  );
  const durations = videoDurationOptionsForModels(
    options,
    nextModels,
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
  const durations = videoDurationOptionsForModels(
    options,
    model.capabilityModels,
    model.action,
    resolution,
  );
  return {
    resolution,
    duration_s: currentOrPreferredDuration(model.currentDuration, durations),
  };
}
