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
  durationOptionsForModel,
  durationOrPreferred,
  firstModelForAction,
  preferredResolution,
  resolutionOptionsForModel,
} from "./video-options-model";

type UseVideoParameterHandlersOptions = {
  action: VideoAction;
  options: VideoOptionsOut | undefined;
  resolution: string;
  selectedModel: string;
  beforeParameterChange: () => void;
  switchDraftContext: (taskId: string, action: VideoAction) => void;
  setAction: Dispatch<SetStateAction<VideoAction>>;
  setAspectRatio: Dispatch<SetStateAction<string>>;
  setDurationS: Dispatch<SetStateAction<number>>;
  setGenerateAudio: Dispatch<SetStateAction<boolean>>;
  setModel: Dispatch<SetStateAction<string>>;
  setResolution: Dispatch<SetStateAction<string>>;
};

export function useVideoParameterHandlers({
  action,
  options,
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
      const nextResolutions = resolutionOptionsForModel(options, nextModel);
      const nextResolution = nextResolutions.includes(resolution)
        ? resolution
        : preferredResolution(nextResolutions);
      const nextDurations = durationOptionsForModel(
        options,
        nextModel,
        nextAction,
        nextResolution,
      );
      setAction(nextAction);
      setModel(nextModel);
      setDurationS((previous) =>
        durationOrPreferred(previous, nextDurations),
      );
    },
    [
      options,
      resolution,
      setAction,
      setDurationS,
      setModel,
      switchDraftContext,
    ],
  );

  const handleModelChange = useCallback(
    (value: string) => {
      beforeParameterChange();
      const nextResolutions = resolutionOptionsForModel(options, value);
      const nextResolution = nextResolutions.includes(resolution)
        ? resolution
        : preferredResolution(nextResolutions);
      const nextDurations = durationOptionsForModel(
        options,
        value,
        action,
        nextResolution,
      );
      setModel(value);
      setResolution(nextResolution);
      setDurationS((previous) =>
        durationOrPreferred(previous, nextDurations),
      );
    },
    [
      action,
      beforeParameterChange,
      options,
      resolution,
      setDurationS,
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
        durationOrPreferred(previous, nextDurations),
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
