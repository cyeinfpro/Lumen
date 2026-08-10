import type {
  VideoAction,
  VideoModelOptionOut,
  VideoOptionsOut,
} from "@/lib/types";

import {
  referenceAllowsAudioOnlyForModelOption,
  referenceLimitsForModelOption,
  referenceTotalLimitForModelOption,
} from "./video-reference-domain";
import type { ReferenceLimits } from "./video-reference-domain";
import {
  aspectRatioOptionsForModel,
  audioCapabilityForModel,
  billingModelForAction,
  defaultAspectRatioForModel,
  defaultDurationForModel,
  defaultResolutionForModel,
  durationOptionsForModel,
  generateAudioOrDefault,
  imageConstraintsForModel,
  resolutionOptionsForModel,
  selectedVideoAction,
  videoActionsForOptions,
  videoModelsForAction,
} from "./video-options-model";
import type {
  NormalizedVideoImageConstraints,
  VideoAudioCapability,
} from "./video-options-model";
import {
  effectiveVideoAspectRatio,
  effectiveVideoDuration,
  effectiveVideoResolution,
  selectedVideoModel,
} from "./video-page-domain";

export type VideoOptionSelection = {
  availableActions: VideoAction[];
  effectiveAction: VideoAction;
  availableModels: VideoModelOptionOut[];
  selectedModel: string;
  selectedModelOption: VideoModelOptionOut | undefined;
  referenceLimits: ReferenceLimits;
  referenceTotalLimit: number | null;
  allowAudioOnlyReference: boolean;
  inputImageConstraints: NormalizedVideoImageConstraints | null;
  referenceImageConstraints: NormalizedVideoImageConstraints | null;
};

export function deriveVideoOptionSelection(
  options: VideoOptionsOut | undefined,
  requestedAction: VideoAction,
  requestedModel: string,
): VideoOptionSelection {
  const availableActions = videoActionsForOptions(options);
  const effectiveAction = selectedVideoAction(options, requestedAction);
  const availableModels = videoModelsForAction(options, effectiveAction);
  const modelCandidate = availableModels.some(
    (item) => item.model === requestedModel,
  )
    ? requestedModel
    : options?.default_model ?? "";
  const selectedModel = selectedVideoModel(availableModels, modelCandidate);
  const selectedModelOption = availableModels.find(
    (item) => item.model === selectedModel,
  );
  return {
    availableActions,
    effectiveAction,
    availableModels,
    selectedModel,
    selectedModelOption,
    referenceLimits: referenceLimitsForModelOption(selectedModelOption),
    referenceTotalLimit:
      referenceTotalLimitForModelOption(selectedModelOption),
    allowAudioOnlyReference:
      referenceAllowsAudioOnlyForModelOption(selectedModelOption),
    inputImageConstraints: imageConstraintsForModel(
      options,
      selectedModel,
      effectiveAction,
      "input",
    ),
    referenceImageConstraints: imageConstraintsForModel(
      options,
      selectedModel,
      effectiveAction,
      "reference",
    ),
  };
}

export type VideoParameterSelection = {
  selectedBillingModel: string;
  availableResolutions: string[];
  effectiveResolution: string;
  availableAspectRatios: string[];
  effectiveAspectRatio: string;
  availableDurations: number[];
  effectiveDurationS: number;
  audioCapability: VideoAudioCapability;
  effectiveGenerateAudio: boolean;
};

export function deriveVideoParameterSelection(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
  requested: {
    resolution: string;
    aspectRatio: string;
    durationS: number | null;
    generateAudio: boolean | null;
  },
): VideoParameterSelection {
  const selectedBillingModel = billingModelForAction(options, model, action);
  const availableResolutions = resolutionOptionsForModel(
    options,
    model,
    action,
  );
  const effectiveResolution = effectiveVideoResolution(
    availableResolutions,
    requested.resolution,
    defaultResolutionForModel(options, model, action),
  );
  const availableAspectRatios = aspectRatioOptionsForModel(
    options,
    model,
    action,
  );
  const effectiveAspectRatio = effectiveVideoAspectRatio(
    availableAspectRatios,
    requested.aspectRatio,
    defaultAspectRatioForModel(options, model, action),
  );
  const availableDurations = durationOptionsForModel(
    options,
    model,
    action,
    effectiveResolution,
  );
  const effectiveDurationS = effectiveVideoDuration(
    availableDurations,
    requested.durationS,
    defaultDurationForModel(options, model, action),
  );
  const audioCapability = audioCapabilityForModel(options, model, action);
  return {
    selectedBillingModel,
    availableResolutions,
    effectiveResolution,
    availableAspectRatios,
    effectiveAspectRatio,
    availableDurations,
    effectiveDurationS,
    audioCapability,
    effectiveGenerateAudio: generateAudioOrDefault(
      requested.generateAudio,
      audioCapability,
    ),
  };
}
