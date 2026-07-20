import type { Dispatch, SetStateAction } from "react";

import type { Stroke, Tool } from "./types";

export const MASK_BRUSH_STEP = 4;

type MaskShortcut =
  | { type: "tool"; tool: Tool }
  | { type: "undo" }
  | { type: "brush-step"; delta: number }
  | { type: "brush-size"; size: number };

const BRUSH_PRESETS: Record<string, number> = {
  "1": 12,
  "2": 18,
  "3": 26,
  "4": 36,
  "5": 48,
  "6": 60,
  "7": 72,
  "8": 84,
  "9": 96,
};

const MASK_SHORTCUTS: Record<string, MaskShortcut> = {
  b: { type: "tool", tool: "brush" },
  e: { type: "tool", tool: "eraser" },
  z: { type: "undo" },
  "[": { type: "brush-step", delta: -MASK_BRUSH_STEP },
  "]": { type: "brush-step", delta: MASK_BRUSH_STEP },
};

export function shouldIgnoreMaskShortcut(event: KeyboardEvent): boolean {
  if (event.metaKey || event.ctrlKey || event.altKey) return true;
  const target = event.target as HTMLElement | null;
  if (!target) return false;
  return (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.isContentEditable
  );
}

export function resolveMaskShortcut(key: string): MaskShortcut | null {
  const preset = BRUSH_PRESETS[key];
  if (preset) return { type: "brush-size", size: preset };
  return MASK_SHORTCUTS[key.toLowerCase()] ?? null;
}

export function applyMaskShortcut(
  shortcut: MaskShortcut,
  hasStroke: boolean,
  actions: {
    setTool: (tool: Tool) => void;
    setStrokes: Dispatch<SetStateAction<Stroke[]>>;
    setBrushSize: Dispatch<SetStateAction<number>>;
    clampBrush: (value: number) => number;
  },
): boolean {
  switch (shortcut.type) {
    case "tool":
      actions.setTool(shortcut.tool);
      return true;
    case "undo":
      if (!hasStroke) return false;
      actions.setStrokes((prev) => prev.slice(0, -1));
      return true;
    case "brush-step":
      actions.setBrushSize((value) =>
        actions.clampBrush(value + shortcut.delta),
      );
      return true;
    case "brush-size":
      actions.setBrushSize(shortcut.size);
      return true;
  }
}
