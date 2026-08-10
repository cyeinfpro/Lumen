"use client";

import { useCallback } from "react";
import type {
  Dispatch,
  SetStateAction,
} from "react";

import type {
  VideoAction,
  VideoOptionsOut,
} from "@/lib/types";

import {
  aspectRatioOptionsForModel,
  audioCapabilityForModel,
  defaultAspectRatioForModel,
  defaultDurationForModel,
  defaultResolutionForModel,
  durationOptionsForModel,
  durationOrPreferred,
  firstModelForAction,
  generateAudioOrDefault,
  resolutionOptionsForModel,
  stringOrPreferred,
} from "./video-options-model";

type UseVideoParameterHandlersOptions = {
  action: VideoAction;
  options: VideoOptionsOut | undefined;
  aspectRatio: string;
  resolution: string;
  selectedModel: string;
  beforeParameterChange: () => void;
  switchDraftContext: (taskId: string, action: VideoAction) => void;
  setAction: Dispatch<SetStateAction<VideoAction>>;
  setAspectRatio: Dispatch<SetStateAction<string>>;
  setDurationS: Dispatch<SetStateAction<number | null>>;
  setGenerateAudio: Dispatch<SetStateAction<boolean | null>>;
  setModel: Dispatch<SetStateAction<string>>;
  setResolution: Dispatch<SetStateAction<string>>;
};

export function useVideoParameterHandlers({
  action,
  options,
  aspectRatio,
  resolution,
  selectedModel,
  beforeParameterChange,
  switchDraftContext,
  setAction,
  setAspectRatio,
  setDurationS,
  setGenerateAudio,
  setModel,
  setResolution,
}: UseVideoParameterHandlersOptions) {
  const handleActionChange = useCallback(
    (nextAction: VideoAction) => {
      switchDraftContext(`draft:${nextAction}`, nextAction);
      const nextModel = firstModelForAction(options, nextAction);
      const nextResolutions = resolutionOptionsForModel(
        options,
        nextModel,
        nextAction,
      );
      const nextResolution = stringOrPreferred(
        resolution,
        nextResolutions,
        defaultResolutionForModel(options, nextModel, nextAction),
      );
      const nextDurations = durationOptionsForModel(
        options,
        nextModel,
        nextAction,
        nextResolution,
      );
      const nextAspectRatios = aspectRatioOptionsForModel(
        options,
        nextModel,
        nextAction,
      );
      setAction(nextAction);
      setModel(nextModel);
      setResolution(nextResolution);
      setAspectRatio(
        stringOrPreferred(
          aspectRatio,
          nextAspectRatios,
          defaultAspectRatioForModel(options, nextModel, nextAction),
        ),
      );
      setDurationS((previous) =>
        durationOrPreferred(
          previous,
          nextDurations,
          defaultDurationForModel(options, nextModel, nextAction),
        ),
      );
      setGenerateAudio((previous) =>
        generateAudioOrDefault(
          previous,
          audioCapabilityForModel(options, nextModel, nextAction),
        ),
      );
    },
    [
      aspectRatio,
      options,
      resolution,
      setAction,
      setAspectRatio,
      setDurationS,
      setGenerateAudio,
      setModel,
      setResolution,
      switchDraftContext,
    ],
  );

  const handleModelChange = useCallback(
    (value: string) => {
      beforeParameterChange();
      const nextResolutions = resolutionOptionsForModel(
        options,
        value,
        action,
      );
      const nextResolution = stringOrPreferred(
        resolution,
        nextResolutions,
        defaultResolutionForModel(options, value, action),
      );
      const nextDurations = durationOptionsForModel(
        options,
        value,
        action,
        nextResolution,
      );
      const nextAspectRatios = aspectRatioOptionsForModel(
        options,
        value,
        action,
      );
      setModel(value);
      setResolution(nextResolution);
      setAspectRatio(
        stringOrPreferred(
          aspectRatio,
          nextAspectRatios,
          defaultAspectRatioForModel(options, value, action),
        ),
      );
      setDurationS((previous) =>
        durationOrPreferred(
          previous,
          nextDurations,
          defaultDurationForModel(options, value, action),
        ),
      );
      setGenerateAudio((previous) =>
        generateAudioOrDefault(
          previous,
          audioCapabilityForModel(options, value, action),
        ),
      );
    },
    [
      action,
      aspectRatio,
      beforeParameterChange,
      options,
      resolution,
      setAspectRatio,
      setDurationS,
      setGenerateAudio,
      setModel,
      setResolution,
    ],
  );

  const handleDurationChange = useCallback(
    (value: string) => {
      beforeParameterChange();
      setDurationS(Number(value));
    },
    [beforeParameterChange, setDurationS],
  );

  const handleResolutionChange = useCallback(
    (value: string) => {
      beforeParameterChange();
      const nextDurations = durationOptionsForModel(
        options,
        selectedModel,
        action,
        value,
      );
      setResolution(value);
      setDurationS((previous) =>
        durationOrPreferred(
          previous,
          nextDurations,
          defaultDurationForModel(options, selectedModel, action),
        ),
      );
    },
    [
      action,
      beforeParameterChange,
      options,
      selectedModel,
      setDurationS,
      setResolution,
    ],
  );

  const handleAspectRatioChange = useCallback(
    (value: string) => {
      beforeParameterChange();
      setAspectRatio(value);
    },
    [beforeParameterChange, setAspectRatio],
  );

  const handleGenerateAudioChange = useCallback(
    (value: boolean) => {
      beforeParameterChange();
      setGenerateAudio(value);
    },
    [beforeParameterChange, setGenerateAudio],
  );

  return {
    handleActionChange,
    handleAspectRatioChange,
    handleDurationChange,
    handleGenerateAudioChange,
    handleModelChange,
    handleResolutionChange,
  };
}
