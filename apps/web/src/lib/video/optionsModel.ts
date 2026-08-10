import type {
  VideoAction,
  VideoActionCapabilityOut,
  VideoImageConstraintsOut,
  VideoModelOptionOut,
  VideoOptionsOut,
  VideoPricingAction,
  VideoReferenceMediaIn,
} from "../types";

export const SMART_VIDEO_DURATION = -1;
const VIDEO_SEED_MIN = -1;
const VIDEO_SEED_MAX = 4_294_967_295;

export type VideoReferenceCounts = Record<
  VideoReferenceMediaIn["kind"],
  number
>;

export type VideoAudioCapability = {
  supported: boolean;
  defaultValue: boolean;
};

export type VideoReferenceCapability = {
  limits: VideoReferenceCounts;
  totalLimit: number | null;
  allowAudioOnly: boolean;
};

export type NormalizedVideoImageConstraints = {
  minWidthPx?: number;
  maxWidthPx?: number;
  minHeightPx?: number;
  maxHeightPx?: number;
  minAspectRatio?: number;
  maxAspectRatio?: number;
  maxBytes?: number;
  mimeTypes: string[];
};

export type VideoEstimate = {
  tokens: number;
  micro: number;
  unitPriceMicro: number;
  pricingAction: VideoPricingAction;
  note?: string | null;
};

export function videoUnavailableReasonMessage(
  reason: string | null | undefined,
): string {
  if (reason === "account_mode_forbidden") {
    return "BYOK 模式暂不支持视频生成";
  }
  return reason?.trim() || "视频生成功能当前不可用";
}

export function toVideoResolution(value: string): string {
  return value.trim();
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

function uniqueStrings(values: readonly string[] | undefined): string[] {
  if (!values) return [];
  return Array.from(
    new Set(values.map((value) => value.trim()).filter(Boolean)),
  );
}

function uniqueNumbers(values: readonly number[] | undefined): number[] {
  if (!values) return [];
  return Array.from(
    new Set(
      values.filter(
        (value) => Number.isInteger(value) && value >= SMART_VIDEO_DURATION,
      ),
    ),
  );
}

function modelOption(
  options: VideoOptionsOut | undefined,
  model: string,
): VideoModelOptionOut | undefined {
  return options?.models.find((item) => item.model === model);
}

function actionCapability(
  option: VideoModelOptionOut | undefined,
  action: VideoAction,
): VideoActionCapabilityOut | undefined {
  return option?.action_capabilities?.[action] ?? option?.capabilities?.[action];
}

export function modelSupportsVideoAction(
  option: VideoModelOptionOut,
  action: VideoAction,
): boolean {
  const capability = actionCapability(option, action);
  if (capability?.enabled === false) return false;
  return option.actions.includes(action) || capability !== undefined;
}

export function videoActionsForOptions(
  options: VideoOptionsOut | undefined,
): VideoAction[] {
  const declared = options?.actions ?? [];
  const candidates = [
    ...declared,
    ...(options?.models.flatMap((item) => [
      ...item.actions,
      ...(Object.keys(item.action_capabilities ?? {}) as VideoAction[]),
      ...(Object.keys(item.capabilities ?? {}) as VideoAction[]),
    ]) ?? []),
  ];
  return Array.from(
    new Set(
      candidates.filter((action) =>
        options?.models.some((item) => modelSupportsVideoAction(item, action)),
      ),
    ),
  );
}

export function selectedVideoAction(
  options: VideoOptionsOut | undefined,
  requested: VideoAction,
): VideoAction {
  const actions = videoActionsForOptions(options);
  if (actions.includes(requested)) return requested;
  const declaredDefault = options?.default_action;
  if (declaredDefault && actions.includes(declaredDefault)) {
    return declaredDefault;
  }
  return actions[0] ?? requested;
}

export function videoModelLabel(option: VideoModelOptionOut): string {
  return (
    option.display_name?.trim() ||
    option.label?.trim() ||
    option.model
  );
}

export function firstModelForAction(
  options: VideoOptionsOut | undefined,
  action: VideoAction,
  referenceCounts?: VideoReferenceCounts,
): string {
  const models = videoModelsForAction(options, action, referenceCounts);
  const declaredDefault = options?.default_model?.trim();
  if (declaredDefault && models.some((item) => item.model === declaredDefault)) {
    return declaredDefault;
  }
  return models[0]?.model ?? "";
}

function normalizeReferenceLimit(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : 0;
}

export function referenceCapabilityForModelOption(
  option: VideoModelOptionOut | null | undefined,
): VideoReferenceCapability {
  if (!option) {
    return {
      limits: { image: 0, video: 0, audio: 0 },
      totalLimit: null,
      allowAudioOnly: false,
    };
  }
  const capability = actionCapability(option, "reference")?.reference_media;
  const rawLimits = capability?.limits ?? option.reference_media_limits;
  const limits = {
    image: normalizeReferenceLimit(rawLimits?.image),
    video: normalizeReferenceLimit(rawLimits?.video),
    audio: normalizeReferenceLimit(rawLimits?.audio),
  };
  const totalRaw =
    capability?.total_limit ?? option.reference_media_total_limit;
  const totalParsed = Number(totalRaw);
  return {
    limits,
    totalLimit:
      Number.isFinite(totalParsed) && totalParsed >= 0
        ? Math.floor(totalParsed)
        : null,
    allowAudioOnly:
      capability?.allow_audio_only ??
      option.allow_audio_only_reference ??
      false,
  };
}

export function videoReferenceLimitError(
  option: VideoModelOptionOut,
  counts: VideoReferenceCounts,
): string | null {
  const labels = {
    image: "参考图片",
    video: "参考视频",
    audio: "参考音频",
  } as const;
  const policy = referenceCapabilityForModelOption(option);
  for (const kind of ["image", "video", "audio"] as const) {
    const count = counts[kind];
    if (count <= 0) continue;
    const limit = policy.limits[kind];
    if (limit <= 0) return `当前视频模型不支持${labels[kind]}`;
    if (count > limit) {
      return `当前视频模型最多支持 ${limit} 个${labels[kind]}`;
    }
  }
  const total = counts.image + counts.video + counts.audio;
  if (policy.totalLimit !== null && total > policy.totalLimit) {
    return `当前视频模型最多支持 ${policy.totalLimit} 个参考素材`;
  }
  if (
    counts.audio > 0 &&
    counts.image + counts.video === 0 &&
    !policy.allowAudioOnly
  ) {
    return "当前视频模型不支持仅使用参考音频";
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
        modelSupportsVideoAction(item, action) &&
        (!referenceCounts ||
          action !== "reference" ||
          videoReferenceLimitError(item, referenceCounts) === null),
    ) ?? []
  );
}

export function resolutionOptionsForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action?: VideoAction,
): string[] {
  const option = modelOption(options, model);
  const capability = action ? actionCapability(option, action) : undefined;
  const candidates = [
    capability?.resolutions,
    action ? option?.resolutions_by_action?.[action] : undefined,
    option?.resolutions,
    options?.resolutions,
  ];
  for (const candidate of candidates) {
    const values = uniqueStrings(candidate);
    if (values.length > 0) return values;
  }
  return [];
}

export function aspectRatioOptionsForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
): string[] {
  const option = modelOption(options, model);
  const candidates = [
    actionCapability(option, action)?.aspect_ratios,
    option?.aspect_ratios_by_action?.[action],
    option?.aspect_ratios,
    options?.aspect_ratios,
  ];
  for (const candidate of candidates) {
    const values = uniqueStrings(candidate);
    if (values.length > 0) return values;
  }
  return [];
}

export function durationOptionsForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
  resolution: string,
): number[] {
  const option = modelOption(options, model);
  const capability = actionCapability(option, action);
  const candidates = [
    capability?.durations_by_resolution?.[resolution],
    option?.durations_by_action_resolution?.[action]?.[resolution],
    capability?.durations_s,
    option?.durations_by_action?.[action],
    option?.durations_s,
    options?.durations_s,
  ];
  for (const candidate of candidates) {
    const values = uniqueNumbers(candidate);
    if (values.length > 0) return values;
  }
  return [];
}

function defaultsForModelAction(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
) {
  const option = modelOption(options, model);
  return {
    action: actionCapability(option, action)?.defaults,
    modelAction: option?.defaults_by_action?.[action],
    model: option?.defaults,
    global: options?.defaults,
  };
}

export function defaultResolutionForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
): string {
  const defaults = defaultsForModelAction(options, model, action);
  return (
    defaults.action?.resolution?.trim() ||
    defaults.modelAction?.resolution?.trim() ||
    defaults.model?.resolution?.trim() ||
    defaults.global?.resolution?.trim() ||
    ""
  );
}

export function defaultAspectRatioForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
): string {
  const defaults = defaultsForModelAction(options, model, action);
  return (
    defaults.action?.aspect_ratio?.trim() ||
    defaults.modelAction?.aspect_ratio?.trim() ||
    defaults.model?.aspect_ratio?.trim() ||
    defaults.global?.aspect_ratio?.trim() ||
    ""
  );
}

export function defaultDurationForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
): number | null {
  const defaults = defaultsForModelAction(options, model, action);
  for (const value of [
    defaults.action?.duration_s,
    defaults.modelAction?.duration_s,
    defaults.model?.duration_s,
    defaults.global?.duration_s,
  ]) {
    if (Number.isInteger(value) && Number(value) >= SMART_VIDEO_DURATION) {
      return Number(value);
    }
  }
  return null;
}

function firstDeclaredBoolean(
  values: Array<boolean | null | undefined>,
  fallback: boolean,
): boolean {
  const declared = values.find(
    (value): value is boolean => typeof value === "boolean",
  );
  return declared ?? fallback;
}

export function audioCapabilityForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
): VideoAudioCapability {
  const option = modelOption(options, model);
  const capability = actionCapability(option, action);
  const supported = firstDeclaredBoolean(
    [
      capability?.generate_audio,
      option?.generate_audio_by_action?.[action],
      option?.generate_audio,
      options?.generate_audio,
    ],
    false,
  );
  const defaults = defaultsForModelAction(options, model, action);
  const defaultValue = firstDeclaredBoolean(
    [
      defaults.action?.generate_audio,
      defaults.modelAction?.generate_audio,
      defaults.model?.generate_audio,
      defaults.global?.generate_audio,
    ],
    supported,
  );
  return {
    supported,
    defaultValue: supported && defaultValue,
  };
}

export function preferredResolution(
  values: string[],
  declaredDefault?: string | null,
): string {
  const preferred = declaredDefault?.trim();
  return preferred && values.includes(preferred) ? preferred : values[0] ?? "";
}

export function preferredDuration(
  values: number[],
  declaredDefault?: number | null,
): number {
  return declaredDefault != null && values.includes(declaredDefault)
    ? declaredDefault
    : values[0] ?? 0;
}

export function durationOrPreferred(
  current: number | null,
  values: number[],
  declaredDefault?: number | null,
): number {
  return current != null && values.includes(current)
    ? current
    : preferredDuration(values, declaredDefault);
}

export function stringOrPreferred(
  current: string,
  values: string[],
  declaredDefault?: string | null,
): string {
  return values.includes(current)
    ? current
    : preferredResolution(values, declaredDefault);
}

export function generateAudioOrDefault(
  current: boolean | null,
  capability: VideoAudioCapability,
): boolean {
  if (!capability.supported) return false;
  return current ?? capability.defaultValue;
}

export function billingModelForAction(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
): string {
  const option = modelOption(options, model);
  const capabilityBillingModel =
    actionCapability(option, action)?.billing_model?.trim();
  if (capabilityBillingModel) return capabilityBillingModel;
  const actionBillingModel = option?.billing_models?.[action]?.trim();
  if (actionBillingModel) return actionBillingModel;
  const billingModel = option?.billing_model?.trim();
  return billingModel || model;
}

function positiveInteger(value: unknown): number | undefined {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0
    ? Math.floor(parsed)
    : undefined;
}

function positiveNumber(value: unknown): number | undefined {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
}

function normalizeImageConstraints(
  value: VideoImageConstraintsOut | null | undefined,
): NormalizedVideoImageConstraints | null {
  if (!value) return null;
  const minSide = positiveInteger(value.min_side_px);
  const maxSide = positiveInteger(value.max_side_px);
  const normalized = {
    minWidthPx: positiveInteger(value.min_width_px) ?? minSide,
    maxWidthPx: positiveInteger(value.max_width_px) ?? maxSide,
    minHeightPx: positiveInteger(value.min_height_px) ?? minSide,
    maxHeightPx: positiveInteger(value.max_height_px) ?? maxSide,
    minAspectRatio: positiveNumber(value.min_aspect_ratio),
    maxAspectRatio: positiveNumber(value.max_aspect_ratio),
    maxBytes: positiveInteger(value.max_bytes),
    mimeTypes: uniqueStrings(value.mime_types),
  };
  return Object.values(normalized).some((item) =>
    Array.isArray(item) ? item.length > 0 : item !== undefined,
  )
    ? normalized
    : null;
}

export function imageConstraintsForModel(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
  purpose: "input" | "reference",
): NormalizedVideoImageConstraints | null {
  const option = modelOption(options, model);
  const capability = actionCapability(option, action);
  const candidates =
    purpose === "reference"
      ? [
          capability?.reference_image_constraints,
          capability?.reference_media?.image_constraints,
          option?.reference_image_constraints,
          options?.reference_image_constraints,
          capability?.input_image_constraints,
          option?.input_image_constraints,
          options?.input_image_constraints,
        ]
      : [
          capability?.input_image_constraints,
          option?.input_image_constraints,
          options?.input_image_constraints,
        ];
  for (const candidate of candidates) {
    const normalized = normalizeImageConstraints(candidate);
    if (normalized) return normalized;
  }
  return null;
}

function smartEstimateDuration(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
  resolution: string,
  durationS: number,
): number {
  if (durationS !== SMART_VIDEO_DURATION) return durationS;
  const positiveDurations = durationOptionsForModel(
    options,
    model,
    action,
    resolution,
  ).filter((value) => value > 0);
  return positiveDurations.length > 0 ? Math.max(...positiveDurations) : durationS;
}

function referencePricingAction(
  counts: VideoReferenceCounts,
): VideoPricingAction {
  if (counts.video > 0) return "reference_video";
  if (counts.image > 0) return "reference_image";
  if (counts.audio > 0) return "reference_audio";
  return "reference";
}

function pricingActionCandidates(
  options: VideoOptionsOut | undefined,
  {
    model,
    billingModel,
    action,
    resolution,
    referenceCounts,
  }: {
    model: string;
    billingModel: string;
    action: VideoAction;
    resolution: string;
    referenceCounts: VideoReferenceCounts;
  },
): VideoPricingAction[] {
  const option = modelOption(options, model);
  const configured = actionCapability(option, action)?.pricing_action;
  const requested =
    action === "reference" ? referencePricingAction(referenceCounts) : action;
  const modelCandidates = new Set([billingModel, model]);
  const discovered =
    options?.pricing
      .filter(
        (item) =>
          item.enabled &&
          modelCandidates.has(item.model) &&
          (!item.resolution || item.resolution === resolution) &&
          (action !== "reference" ||
            (referenceCounts.video > 0
              ? item.action === "reference_video" ||
                item.action === "reference"
              : item.action !== "reference_video")),
      )
      .map((item) => item.action) ?? [];
  return Array.from(
    new Set(
      [configured, requested, action, ...discovered].filter(
        (value): value is VideoPricingAction => Boolean(value),
      ),
    ),
  );
}

function findHoldEstimateTokens(
  options: VideoOptionsOut | undefined,
  modelCandidates: string[],
  actions: VideoPricingAction[],
  estimateKey: string,
): { action: VideoPricingAction; tokens: number } | null {
  for (const modelCandidate of modelCandidates) {
    const tokenMap = options?.hold_estimates?.[modelCandidate];
    if (!tokenMap || typeof tokenMap !== "object") continue;
    const tokenRecord = tokenMap as Record<string, unknown>;
    for (const action of actions) {
      const actionMap = tokenRecord[action];
      if (!actionMap || typeof actionMap !== "object") continue;
      const tokens = Number(
        (actionMap as Record<string, unknown>)[estimateKey],
      );
      if (Number.isFinite(tokens) && tokens > 0) return { action, tokens };
    }
  }
  return null;
}

function findVideoPrice(
  options: VideoOptionsOut | undefined,
  modelCandidates: string[],
  actions: VideoPricingAction[],
  resolution: string,
): VideoOptionsOut["pricing"][number] | undefined {
  for (const action of actions) {
    for (const modelCandidate of modelCandidates) {
      const exact = options?.pricing.find(
        (item) =>
          item.model === modelCandidate &&
          item.action === action &&
          item.resolution === resolution &&
          item.enabled,
      );
      if (exact) return exact;
      const generic = options?.pricing.find(
        (item) =>
          item.model === modelCandidate &&
          item.action === action &&
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
    referenceCounts,
    referenceHasVideo,
  }: {
    model: string;
    billingModel?: string;
    action: VideoAction;
    resolution: string;
    durationS: number;
    referenceCounts?: VideoReferenceCounts;
    referenceHasVideo?: boolean;
  },
): VideoEstimate | null {
  const resolvedBillingModel = billingModel?.trim() || model;
  const modelCandidates = Array.from(
    new Set([resolvedBillingModel, model].filter(Boolean)),
  );
  const counts = referenceCounts ?? {
    image: 0,
    video: referenceHasVideo ? 1 : 0,
    audio: 0,
  };
  const actions = pricingActionCandidates(options, {
    model,
    billingModel: resolvedBillingModel,
    action,
    resolution,
    referenceCounts: counts,
  });
  const estimateDuration = smartEstimateDuration(
    options,
    model,
    action,
    resolution,
    durationS,
  );
  const estimateKey = `${resolution}:${estimateDuration}`;
  const hold = findHoldEstimateTokens(
    options,
    modelCandidates,
    actions,
    estimateKey,
  );
  if (!hold) return null;
  const orderedPriceActions = [
    hold.action,
    ...actions.filter((item) => item !== hold.action),
  ];
  const price = findVideoPrice(
    options,
    modelCandidates,
    orderedPriceActions,
    resolution,
  );
  if (!price) return null;
  return {
    tokens: hold.tokens,
    micro: Math.round((hold.tokens * price.price.micro) / 1_000_000),
    unitPriceMicro: price.price.micro,
    pricingAction: price.action,
    note: price.note,
  };
}
