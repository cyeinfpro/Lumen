"use client";

import { Check } from "lucide-react";

import type { StoryboardRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";

import {
  isStageUnlocked,
  STAGES,
  stageCompletion,
  type StoryboardStage,
} from "./StoryboardDomain";

export function StageRail({
  run,
  activeStage,
  onSelect,
}: {
  run: StoryboardRun;
  activeStage: StoryboardStage;
  onSelect: (stage: StoryboardStage) => void;
}) {
  return (
    <aside
      aria-label="分镜步骤"
      className="scrollbar-none min-h-0 shrink-0 overflow-x-auto border-b border-[var(--border)] p-2 md:overflow-x-hidden md:overflow-y-auto md:border-b-0 md:p-3"
    >
      <div className="flex w-max gap-2 md:grid md:w-auto">
        {STAGES.map((stage, index) => {
          const meta = stageCompletion(run, stage.id);
          const unlocked = isStageUnlocked(run, stage.id);
          const active = activeStage === stage.id;
          return (
            <button
              key={stage.id}
              type="button"
              onClick={() => onSelect(stage.id)}
              disabled={!unlocked}
              aria-current={active ? "step" : undefined}
              className={cn(
                "grid min-h-14 min-w-[116px] shrink-0 gap-1 rounded-[var(--radius-card)] border px-3 py-2 text-left transition md:min-h-[76px] md:min-w-0 md:p-3",
                active
                  ? "border-accent-border bg-[var(--accent-soft)]"
                  : "border-[var(--border)] bg-[var(--bg-1)]/74 hover:bg-[var(--bg-2)]",
                !unlocked &&
                  "cursor-not-allowed opacity-55 hover:bg-[var(--bg-1)]/74",
              )}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="type-caption text-[var(--fg-3)]">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span
                  className={cn(
                    "inline-flex h-5 min-w-5 items-center justify-center rounded-full border px-1 type-caption",
                    meta.done
                      ? "border-[var(--success-border)] bg-[var(--success-soft)] text-[var(--success-fg)]"
                      : "border-[var(--border)] text-[var(--fg-2)]",
                  )}
                >
                  {meta.done ? <Check className="h-3 w-3" /> : meta.count}
                </span>
              </span>
              <span className="type-body-sm font-semibold text-[var(--fg-0)]">
                {stage.label}
              </span>
              <span className="hidden line-clamp-1 type-caption text-[var(--fg-2)] md:block">
                {stage.description}
              </span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}
