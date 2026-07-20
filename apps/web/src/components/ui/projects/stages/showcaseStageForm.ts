import { useState } from "react";

import {
  coerceContinuityAnchor,
  coerceSceneStrategy,
  coerceSceneVariety,
} from "../coercers";
import {
  ASPECT_RATIO_LABELS,
  SCENE_ENVIRONMENT_TEMPLATES,
  SHOT_PLAN_DEFAULT,
  TEMPLATE_LABELS,
  coerceOutputCount,
  type CreateAspectRatio,
  type CreateContinuityAnchor,
  type CreateOutputCount,
  type CreateSceneEnvironment,
  type CreateSceneStrategy,
  type CreateSceneVariety,
  type CreateTemplate,
} from "../types";

type StepInput = Record<string, unknown> | null | undefined;

export interface ShowcaseFormValues {
  template: CreateTemplate;
  aspectRatio: CreateAspectRatio;
  quality: "high" | "4k";
  outputCount: CreateOutputCount;
  sceneEnvironment: CreateSceneEnvironment;
  sceneStrategy: CreateSceneStrategy;
  sceneVariety: CreateSceneVariety;
  continuityAnchor: CreateContinuityAnchor;
  allowPet: boolean;
  allowBackgroundPeople: boolean;
}

export interface ShowcaseFormController extends ShowcaseFormValues {
  setTemplate: (value: CreateTemplate) => void;
  setAspectRatio: (value: CreateAspectRatio) => void;
  setQuality: (value: "high" | "4k") => void;
  setOutputCount: (value: CreateOutputCount) => void;
  setSceneEnvironment: (value: CreateSceneEnvironment) => void;
  setSceneStrategy: (value: CreateSceneStrategy) => void;
  setSceneVariety: (value: CreateSceneVariety) => void;
  setContinuityAnchor: (value: CreateContinuityAnchor) => void;
  setAllowPet: (value: boolean) => void;
  setAllowBackgroundPeople: (value: boolean) => void;
}

export function useShowcaseStageForm(
  input: StepInput,
  syncLocked: boolean,
): ShowcaseFormController {
  const initial = readShowcaseFormValues(input);
  const currentConfigKey = showcaseConfigKey(initial);
  const [values, setValues] = useState<ShowcaseFormValues>(initial);
  const [trackedConfigKey, setTrackedConfigKey] = useState(currentConfigKey);

  if (!syncLocked && trackedConfigKey !== currentConfigKey) {
    setTrackedConfigKey(currentConfigKey);
    setValues(initial);
  }

  const setValue = <Key extends keyof ShowcaseFormValues>(
    key: Key,
    value: ShowcaseFormValues[Key],
  ) => {
    setValues((current) => ({ ...current, [key]: value }));
  };

  return {
    ...values,
    setTemplate: (value) => setValue("template", value),
    setAspectRatio: (value) => setValue("aspectRatio", value),
    setQuality: (value) => setValue("quality", value),
    setOutputCount: (value) => setValue("outputCount", value),
    setSceneEnvironment: (value) => setValue("sceneEnvironment", value),
    setSceneStrategy: (value) => setValue("sceneStrategy", value),
    setSceneVariety: (value) => setValue("sceneVariety", value),
    setContinuityAnchor: (value) => {
      setValues((current) => ({
        ...current,
        continuityAnchor: value,
        allowPet: value === "pet" ? true : current.allowPet,
      }));
    },
    setAllowPet: (value) => {
      setValues((current) => updateAllowPet(current, value));
    },
    setAllowBackgroundPeople: (value) =>
      setValue("allowBackgroundPeople", value),
  };
}

export function buildShowcaseRequest(
  values: ShowcaseFormValues,
  options: { forceIndoorForUnsupportedTemplate?: boolean } = {},
) {
  return {
    template: values.template,
    shot_plan: [...SHOT_PLAN_DEFAULT],
    aspect_ratio: values.aspectRatio,
    final_quality: values.quality,
    output_count: values.outputCount,
    scene_environment: resolveSceneEnvironment(values, options),
    scene_strategy: values.sceneStrategy,
    scene_variety: values.sceneVariety,
    scene_planner: "gpt55_preflight" as const,
    continuity_anchor: values.continuityAnchor,
    allow_pet: values.allowPet,
    allow_background_people: values.allowBackgroundPeople,
  };
}

export function sceneEnvironmentEnabled(template: CreateTemplate): boolean {
  return SCENE_ENVIRONMENT_TEMPLATES.has(template);
}

function readShowcaseFormValues(input: StepInput): ShowcaseFormValues {
  const continuityAnchor = coerceContinuityAnchor(input?.continuity_anchor);
  return {
    template: coerceTemplate(input?.template),
    aspectRatio: coerceAspectRatio(input?.aspect_ratio),
    quality: coerceQuality(input?.final_quality),
    outputCount: coerceOutputCount(input?.output_count),
    sceneEnvironment: coerceSceneEnvironment(input?.scene_environment),
    sceneStrategy: coerceSceneStrategy(input?.scene_strategy),
    sceneVariety: coerceSceneVariety(input?.scene_variety),
    continuityAnchor,
    allowPet: booleanValue(input?.allow_pet, continuityAnchor === "pet"),
    allowBackgroundPeople: booleanValue(input?.allow_background_people, true),
  };
}

function showcaseConfigKey(values: ShowcaseFormValues): string {
  return [
    values.template,
    values.aspectRatio,
    values.quality,
    values.outputCount,
    values.sceneEnvironment,
    values.sceneStrategy,
    values.sceneVariety,
    values.continuityAnchor,
    values.allowPet,
    values.allowBackgroundPeople,
  ].join(":");
}

function updateAllowPet(
  values: ShowcaseFormValues,
  allowPet: boolean,
): ShowcaseFormValues {
  return {
    ...values,
    allowPet,
    continuityAnchor:
      !allowPet && values.continuityAnchor === "pet"
        ? "accessory"
        : values.continuityAnchor,
  };
}

function resolveSceneEnvironment(
  values: ShowcaseFormValues,
  options: { forceIndoorForUnsupportedTemplate?: boolean },
): CreateSceneEnvironment {
  if (
    options.forceIndoorForUnsupportedTemplate &&
    !sceneEnvironmentEnabled(values.template)
  ) {
    return "indoor";
  }
  return values.sceneEnvironment;
}

function booleanValue(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function coerceTemplate(value: unknown): CreateTemplate {
  return TEMPLATE_LABELS.some(([option]) => option === value)
    ? (value as CreateTemplate)
    : "premium_studio";
}

function coerceAspectRatio(value: unknown): CreateAspectRatio {
  return ASPECT_RATIO_LABELS.some(([option]) => option === value)
    ? (value as CreateAspectRatio)
    : "4:5";
}

function coerceQuality(value: unknown): "high" | "4k" {
  return value === "4k" ? "4k" : "high";
}

function coerceSceneEnvironment(value: unknown): CreateSceneEnvironment {
  return value === "outdoor" ? "outdoor" : "indoor";
}
