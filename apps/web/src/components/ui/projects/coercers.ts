import {
  CONTINUITY_ANCHOR_LABELS,
  SCENE_STRATEGY_LABELS,
  SCENE_VARIETY_LABELS,
  type CreateContinuityAnchor,
  type CreateSceneStrategy,
  type CreateSceneVariety,
} from "./types";

export function coerceSceneStrategy(value: unknown): CreateSceneStrategy {
  return SCENE_STRATEGY_LABELS.some(([option]) => option === value)
    ? (value as CreateSceneStrategy)
    : "natural_series";
}

export function coerceSceneVariety(value: unknown): CreateSceneVariety {
  return SCENE_VARIETY_LABELS.some(([option]) => option === value)
    ? (value as CreateSceneVariety)
    : "rich";
}

export function coerceContinuityAnchor(value: unknown): CreateContinuityAnchor {
  return CONTINUITY_ANCHOR_LABELS.some(([option]) => option === value)
    ? (value as CreateContinuityAnchor)
    : "accessory";
}
