"use client";

import { GripVertical } from "lucide-react";

import {
  CANVAS_NODE_SPECS,
  CANVAS_NODE_TYPES,
} from "@/lib/canvas/registry";
import type { CanvasNodeType } from "@/lib/canvas/types";

export function CanvasNodePalette({
  onAdd,
  compact = false,
}: {
  onAdd: (type: CanvasNodeType) => void;
  compact?: boolean;
}) {
  return (
    <div className={compact ? "grid grid-cols-2 gap-2" : "grid gap-1.5"}>
      {CANVAS_NODE_TYPES.map((type) => {
        const spec = CANVAS_NODE_SPECS[type];
        const Icon = spec.icon;
        return (
          <button
            key={type}
            type="button"
            draggable={!compact}
            onDragStart={(event) => {
              event.dataTransfer.setData("application/lumen-canvas-node", type);
              event.dataTransfer.effectAllowed = "copy";
            }}
            onClick={() => onAdd(type)}
            className="group flex min-h-11 w-full items-center gap-3 rounded-[var(--radius-control)] border border-transparent px-2.5 text-left transition-colors hover:border-[var(--border)] hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
          >
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[var(--radius-control)] bg-[var(--bg-2)] text-[var(--accent)]">
              <Icon className="h-4 w-4" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate type-body-sm font-medium text-[var(--fg-0)]">
                {spec.label}
              </span>
              <span className="block truncate type-caption text-[var(--fg-2)]">
                {spec.description}
              </span>
            </span>
            {!compact ? (
              <GripVertical className="h-4 w-4 shrink-0 text-[var(--fg-3)] opacity-0 transition-opacity group-hover:opacity-100" />
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
