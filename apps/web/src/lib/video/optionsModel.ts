import type {
  VideoAction,
  VideoCreateIn,
  VideoOptionsOut,
  VideoReferenceMediaIn,
} from "@/lib/types";

export const SMART_VIDEO_DURATION = -1;
const SMART_VIDEO_HOLD_DURATION = 15;
export const VIDEO_DURATION_OPTIONS = [
  SMART_VIDEO_DURATION,
  ...Array.from({ length: 13 }, (_, index) => index + 3),
];
const VIDEO_RESOLUTION_VALUES = new Set<VideoCreateIn["resolution"]>([
  "480p",
  "720p",
  "1080p",
  "4k",
]);
const VIDEO_SEED_MIN = -1;
const VIDEO_SEED_MAX = 4_294_967_295;

export type VideoReferenceCounts = Record<
  VideoReferenceMediaIn["kind"],
  number
>;

export function videoUnavailableReasonMessage(
  reason: string | null | undefined,
): string {
  if (reason === "account_mode_forbidden") {
    return "BYOK 模式暂不支持视频生成";
  }
  return reason?.trim() || "视频生成功能当前不可用";
}

function holdEstimateDurationS(durationS: number): number {
  return durationS === SMART_VIDEO_DURATION
    ? SMART_VIDEO_HOLD_DURATION
    : durationS;
}

export function toVideoResolution(
  value: string,
): VideoCreateIn["resolution"] {
  return VIDEO_RESOLUTION_VALUES.has(value as VideoCreateIn["resolution"])
    ? (value as VideoCreateIn["resolution"])
    : "720p";
}

export function parseSeed(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) &&
    parsed >= VIDEO_SEED_MIN &&
    parsed <= VIDEO_SEED_MAX
    ? parsed
    : null;
}

export function firstModelForAction(
  options: VideoOptionsOut | undefined,
  action: VideoAction,
  referenceCounts?: VideoReferenceCounts,
): string {
  return (
    videoModelsForAction(options, action, referenceCounts)[0]?.model ?? ""
  );
}

export function videoReferenceLimitError(
  model: VideoOptionsOut["models"][number],
  counts: VideoReferenceCounts,
): string | null {
  const labels = {
    image: "参考图片",
    video: "参考视频",
    audio: "参考音频",
  } as const;
  for (const kind of ["image", "video", "audio"] as const) {
    const count = counts[kind];
    if (count <= 0) continue;
    const limit = Number(model.reference_media_limits?.[kind] ?? 0);
    if (limit <= 0) return `当前视频模型不支持${labels[kind]}`;
    if (count > limit) {
      return `当前视频模型最多支持 ${limit} 个${labels[kind]}`;
    }
  }
  return null;
}

export function videoModelsForAction(
  options: VideoOptionsOut | undefined,
  action: VideoAction,
  referenceCounts?: VideoReferenceCounts,
): VideoOptionsOut["models"] {
  return (
    options?.models.filter(
      (item) =>
        item.actions.includes(action) &&
        (!referenceCounts ||
          action !== "reference" ||
          videoReferenceLimitError(item, referenceCounts) === null),
    ) ?? []
  );
}

export function resolutionOptionsForModel(
  options: VideoOptionsOut | undefined,
  model: string,
): string[] {
  const modelOptions = options?.models.find((item) => item.model === model);
  if (modelOptions?.resolutions?.length) return modelOptions.resolutions;
  return options?.resolutions?.length
    ? options.resolutions
    : ["480p", "720p", "1080p"];
}

function firstAvailableDurationOptions(
  candidates: Array<number[] | undefined>,
): number[] {
  for (const candidate of candidates) {
    if (candidate?.length) return candidate;
  }
  return VIDEO_DURATION_OPTIONS;
}

export function durationOptionsForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
  resolution: string,
): number[] {
  const modelOptions = options?.models.find((item) => item.model === model);
  const actionResolutionDurations =
    modelOptions?.durations_by_action_resolution?.[action]?.[resolution];
  const actionDurations = modelOptions?.durations_by_action?.[action];
  return firstAvailableDurationOptions([
    actionResolutionDurations,
    actionDurations,
    modelOptions?.durations_s,
    options?.durations_s,
  ]);
}

export function billingModelForAction(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
): string {
  const modelOptions = options?.models.find((item) => item.model === model);
  const actionBillingModel = modelOptions?.billing_models?.[action]?.trim();
  if (actionBillingModel) return actionBillingModel;
  const billingModel = modelOptions?.billing_model?.trim();
  return billingModel || model;
}

export function preferredResolution(options: string[]): string {
  return options.includes("720p") ? "720p" : options[0] ?? "720p";
}

export function preferredDuration(options: number[]): number {
  return options.includes(5) ? 5 : options[0] ?? 5;
}

export function durationOrPreferred(
  current: number,
  options: number[],
): number {
  return options.includes(current) ? current : preferredDuration(options);
}

type VideoPricingAction = VideoOptionsOut["pricing"][number]["action"];

function estimateActionsFor(
  action: VideoAction,
  referenceHasVideo: boolean,
): string[] {
  if (action !== "reference") return [action];
  return referenceHasVideo
    ? ["reference_video"]
    : ["reference_image", "reference", "i2v", "t2v"];
}

function pricingActionsFor(
  action: VideoAction,
  referenceHasVideo: boolean,
): VideoPricingAction[] {
  if (action !== "reference") return [action];
  return referenceHasVideo
    ? ["reference_video", "reference"]
    : ["reference_image", "reference", "i2v"];
}

function findHoldEstimateTokens(
  options: VideoOptionsOut | undefined,
  modelCandidates: string[],
  estimateActions: string[],
  estimateKey: string,
): unknown {
  for (const modelCandidate of modelCandidates) {
    const tokenMap = options?.hold_estimates?.[modelCandidate];
    if (!tokenMap || typeof tokenMap !== "object") continue;
    const tokenRecord = tokenMap as Record<string, unknown>;
    for (const estimateAction of estimateActions) {
      const actionMap = tokenRecord[estimateAction];
      if (!actionMap || typeof actionMap !== "object") continue;
      const tokens = (actionMap as Record<string, unknown>)[estimateKey];
      if (tokens != null) return tokens;
    }
  }
  return undefined;
}

function findVideoPrice(
  options: VideoOptionsOut | undefined,
  modelCandidates: string[],
  priceActions: VideoPricingAction[],
  resolution: string,
): VideoOptionsOut["pricing"][number] | undefined {
  for (const priceAction of priceActions) {
    for (const modelCandidate of modelCandidates) {
      const exact = options?.pricing.find(
        (item) =>
          item.model === modelCandidate &&
          item.action === priceAction &&
          item.resolution === resolution &&
          item.enabled,
      );
      if (exact) return exact;
      const generic = options?.pricing.find(
        (item) =>
          item.model === modelCandidate &&
          item.action === priceAction &&
          !item.resolution &&
          item.enabled,
      );
      if (generic) return generic;
    }
  }
  return undefined;
}

export function estimateHoldMicro(
  options: VideoOptionsOut | undefined,
  {
    model,
    billingModel,
    action,
    resolution,
    durationS,
    referenceHasVideo,
  }: {
    model: string;
    billingModel?: string;
    action: VideoAction;
    resolution: string;
    durationS: number;
    referenceHasVideo?: boolean;
  },
): { tokens: number; micro: number } | null {
  const modelCandidates = Array.from(
    new Set([billingModel, model].filter(Boolean) as string[]),
  );
  const estimateKey = `${resolution}:${holdEstimateDurationS(durationS)}`;
  const tokensRaw = findHoldEstimateTokens(
    options,
    modelCandidates,
    estimateActionsFor(action, Boolean(referenceHasVideo)),
    estimateKey,
  );
  const tokens = Number(tokensRaw);
  if (!Number.isFinite(tokens) || tokens <= 0) return null;
  const price = findVideoPrice(
    options,
    modelCandidates,
    pricingActionsFor(action, Boolean(referenceHasVideo)),
    resolution,
  );
  if (!price) return null;
  return { tokens, micro: Math.round((tokens * price.price.micro) / 1_000_000) };
}
