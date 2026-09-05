"use client";

import { ChevronDown, Clapperboard, Sparkles } from "lucide-react";
import { useEffect, useRef } from "react";

import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import {
  PromptEnhanceChooser,
  type PromptEnhanceCandidate,
} from "./video-workbench-ui";

export interface VideoPromptEditorModel {
  onPromptEditorChange: (element: HTMLTextAreaElement | null) => void;
  value: string;
  enhancing: boolean;
  canEnhance: boolean;
  uploadsPending: boolean;
  panelVisible: boolean;
  preview: string;
  candidates: PromptEnhanceCandidate[];
  selectedCandidateId: string;
  onEnhance: () => void;
  onChange: (value: string) => void;
  onInsertChip: (value: string) => void;
  onSelectCandidate: (candidate: PromptEnhanceCandidate) => void;
  onDismissCandidates: () => void;
  onReturnToEditor: () => void;
}

const CAMERA_MOVEMENT_LIBRARY = [
  {
    category: "镜头景别",
    chips: ["近景", "特写", "全景"],
  },
  {
    category: "运镜轨迹",
    chips: ["推镜", "拉镜", "跟拍", "转台"],
  },
  {
    category: "光影氛围",
    chips: ["侧光", "自然光", "浅景深", "轻微运动模糊", "干净背景"],
  },
] as const;

export function VideoPromptEditor({
  model,
}: {
  model: VideoPromptEditorModel;
}) {
  const promptEditorRef = useRef<HTMLTextAreaElement | null>(null);
  const { onPromptEditorChange } = model;
  useEffect(() => {
    onPromptEditorChange(promptEditorRef.current);
    return () => onPromptEditorChange(null);
  }, [onPromptEditorChange]);

  return (
    <>
      <section className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/90 shadow-[var(--shadow-1)] transition-colors focus-within:border-[var(--accent-border)]">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/40 px-3.5 py-2.5 sm:px-4">
          <div>
            <p className="type-body-sm font-semibold text-[var(--fg-0)]">镜头描述</p>
            <p className="mt-0.5 type-caption text-[var(--fg-2)]">
              描述主体、动作、运镜与时间推进
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="type-caption font-mono tabular-nums text-[var(--fg-2)]">
              {model.value.length.toLocaleString()} / 10,000
            </span>
            <Button
              variant="secondary"
              size="sm"
              loading={model.enhancing}
              disabled={!model.canEnhance}
              onClick={model.onEnhance}
              leftIcon={<Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" />}
            >
              优化描述
            </Button>
          </div>
        </div>
        <textarea
          ref={promptEditorRef}
          value={model.value}
          onChange={(event) => model.onChange(event.target.value)}
          readOnly={model.enhancing}
          rows={9}
          maxLength={10000}
          placeholder="写清主体、动作轨迹、镜头运动、首尾时间推进；点击参考素材插入 @图片1 / @视频1 来指定素材。"
          className={cn(
            "min-h-[200px] w-full resize-none overflow-y-hidden bg-transparent px-3.5 py-3.5 type-body leading-7 text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] sm:min-h-[320px] sm:px-4 sm:py-4 lg:min-h-[360px] landscape:max-md:min-h-[150px]",
            model.enhancing && "cursor-wait",
          )}
        />
        <div className="border-t border-[var(--border-subtle)] bg-[var(--bg-1)]/60 px-3.5 py-3 sm:px-4">
          <div className="space-y-2.5">
            <div className="flex items-center justify-between gap-3">
              <span className="flex items-center gap-1.5 type-caption font-medium text-[var(--fg-1)]">
                <Clapperboard
                  className="h-3.5 w-3.5 text-[var(--accent)]"
                  aria-hidden="true"
                />
                导演运镜库
              </span>
              <span className="shrink-0 type-caption tabular-nums text-[var(--fg-3)]">
                {CAMERA_MOVEMENT_LIBRARY.length} 组
              </span>
            </div>
            <div className="divide-y divide-[var(--border-subtle)] overflow-hidden rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72">
              {CAMERA_MOVEMENT_LIBRARY.map((group, index) => (
                <details
                  key={group.category}
                  open={index === 0}
                  className="group"
                >
                  <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3 py-2 type-caption font-medium text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]">
                    <span>{group.category}</span>
                    <span className="flex shrink-0 items-center gap-2 text-[var(--fg-3)]">
                      <span className="tabular-nums">{group.chips.length} 项</span>
                      <ChevronDown
                        className="h-3.5 w-3.5 transition-transform group-open:rotate-180"
                        aria-hidden="true"
                      />
                    </span>
                  </summary>
                  <div
                    role="group"
                    aria-label={`${group.category}镜头词`}
                    className="flex flex-wrap gap-1.5 border-t border-[var(--border-subtle)] px-3 py-2.5"
                  >
                    {group.chips.map((chip) => (
                      <button
                        key={chip}
                        type="button"
                        disabled={model.enhancing || model.uploadsPending}
                        onClick={() => model.onInsertChip(chip)}
                        className="inline-flex min-h-11 items-center rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] px-3 type-caption font-medium text-[var(--fg-1)] transition-[background-color,border-color,color,transform] hover:border-[var(--accent-border)] hover:bg-[var(--accent-soft)] hover:text-[var(--accent)] active:scale-[0.98] disabled:pointer-events-none disabled:opacity-50 sm:min-h-8"
                      >
                        <span aria-hidden="true">+&nbsp;</span>
                        {chip}
                      </button>
                    ))}
                  </div>
                </details>
              ))}
            </div>
          </div>
        </div>
      </section>
      {model.panelVisible && (
        <div className="scroll-mt-4 md:scroll-mt-6">
          <PromptEnhanceChooser
            loading={model.enhancing}
            preview={model.preview}
            candidates={model.candidates}
            selectedId={model.selectedCandidateId}
            onSelect={model.onSelectCandidate}
            onDismiss={model.onDismissCandidates}
            onReturnToEditor={model.onReturnToEditor}
          />
        </div>
      )}
    </>
  );
}
