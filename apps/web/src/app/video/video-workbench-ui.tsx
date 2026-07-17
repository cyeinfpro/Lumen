"use client";

/* eslint-disable @next/next/no-img-element -- Reference previews are authenticated API media URLs. */

import { useEffect, useRef, useState } from "react";
import {
  AudioLines,
  ChevronDown,
  CircleCheck,
  Clapperboard,
  Copy,
  Film,
  ImageIcon,
  ListVideo,
  Maximize2,
  PencilLine,
  RefreshCw,
  Send,
  Settings2,
  Sparkles,
  Tags,
  Video as VideoIcon,
  X,
  XCircle,
} from "lucide-react";

import { Button, IconButton, toast } from "@/components/ui/primitives";
import { videoBinaryUrl } from "@/lib/apiClient";
import { formatRmb } from "@/lib/money";
import type { VideoAction, VideoReferenceMediaIn } from "@/lib/types";
import { cn } from "@/lib/utils";

export type ReferenceDraft = VideoReferenceMediaIn & {
  _key: string;
  label: string;
  ref_id: string;
  display: string;
  previewUrl?: string | null;
};

export type PromptEnhanceAction =
  | "direct_pass"
  | "light_refine"
  | "direct_rewrite"
  | "ask_first"
  | "keep_original"
  | "optional_vc";

export type PromptEnhanceCandidate = {
  id: string;
  title: string;
  prompt: string;
  action: PromptEnhanceAction;
};

type ModeCardCopy = {
  title: string;
  eyebrow: string;
};

const SMART_VIDEO_DURATION = -1;
const REFERENCE_REF_ID_RE = /^ref:(image|video|audio):([1-9][0-9]{0,2})$/;
const VIDEO_DIALOG_SELECTOR = '[role="dialog"][aria-modal="true"]';
const VIDEO_DIALOG_FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';

function openVideoDialogs(): HTMLElement[] {
  if (typeof document === "undefined") return [];
  return Array.from(
    document.querySelectorAll<HTMLElement>(VIDEO_DIALOG_SELECTOR),
  ).filter((dialog) => dialog.isConnected);
}

export function isTopmostVideoDialog(dialog: HTMLElement | null): boolean {
  if (!dialog?.isConnected) return false;
  const dialogs = openVideoDialogs();
  return dialogs[dialogs.length - 1] === dialog;
}

export function focusVideoWorkbenchElement(
  target: HTMLElement | null,
  options?: FocusOptions,
  blocked = false,
): boolean {
  if (!target?.isConnected || blocked) return false;
  const dialogs = openVideoDialogs();
  const topmostDialog = dialogs[dialogs.length - 1];
  if (topmostDialog && !topmostDialog.contains(target)) return false;
  target.focus(options);
  return true;
}

export function restoreVideoWorkbenchFocus(
  previousFocus: HTMLElement | null,
  closingDialog: HTMLElement | null,
): void {
  if (typeof window === "undefined") return;
  window.requestAnimationFrame(() => {
    if (!previousFocus?.isConnected) return;
    const otherDialogOpen = openVideoDialogs().some(
      (dialog) => dialog !== closingDialog,
    );
    if (otherDialogOpen) return;
    const active = document.activeElement;
    if (
      active instanceof HTMLElement &&
      active !== document.body &&
      active.isConnected &&
      !closingDialog?.contains(active)
    ) {
      return;
    }
    previousFocus.focus({ preventScroll: true });
  });
}

export function trapVideoDialogFocus(
  event: KeyboardEvent,
  dialog: HTMLElement | null,
): void {
  if (event.key !== "Tab" || !dialog) return;
  const focusable = Array.from(
    dialog.querySelectorAll<HTMLElement>(VIDEO_DIALOG_FOCUSABLE),
  ).filter((element) => element.offsetParent !== null);
  if (focusable.length === 0) {
    event.preventDefault();
    dialog.focus({ preventScroll: true });
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !dialog.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

function formatDurationLabel(durationS: number): string {
  return durationS === SMART_VIDEO_DURATION ? "自动时长" : `${durationS}s`;
}

export function cleanPromptEnhanceText(value: string): string {
  return value
    .replace(/\r\n/g, "\n")
    .replace(/^```(?:json|text)?\s*/i, "")
    .replace(/\s*```$/i, "")
    .replace(/^(?:提示词|prompt)\s*[:：]\s*/i, "")
    .trim()
    .replace(/^["“]|["”]$/g, "")
    .trim();
}

export function shouldAutoApplyPromptEnhanceCandidate(
  candidate: PromptEnhanceCandidate,
): boolean {
  return (
    candidate.action === "direct_pass" || candidate.action === "light_refine"
  );
}

export function canApplyPromptEnhanceCandidate(
  candidate: PromptEnhanceCandidate,
): boolean {
  return (
    candidate.action !== "ask_first" && candidate.action !== "keep_original"
  );
}

function promptEnhanceCandidateButtonText(
  candidate: PromptEnhanceCandidate,
  selected: boolean,
): string {
  if (!canApplyPromptEnhanceCandidate(candidate)) return "仅查看";
  if (selected) return "已应用";
  return "应用此版本";
}

function cleanReferencePreviewUrl(
  value: string | null | undefined,
): string | null {
  const clean = value?.trim();
  if (!clean || /^asset:\/\//i.test(clean)) return null;
  return clean;
}

function referenceKindNoun(kind: VideoReferenceMediaIn["kind"]): string {
  if (kind === "image") return "图片";
  if (kind === "audio") return "音频";
  return "视频";
}

function referenceRefId(
  kind: VideoReferenceMediaIn["kind"],
  index: number,
): string {
  return `ref:${kind}:${index}`;
}

function referenceRefIndex(
  refId: string | null | undefined,
  kind: VideoReferenceMediaIn["kind"],
): number | null {
  const match = (refId ?? "").trim().toLowerCase().match(REFERENCE_REF_ID_RE);
  if (!match || match[1] !== kind) return null;
  const index = Number(match[2]);
  return Number.isInteger(index) && index > 0 ? index : null;
}

function referencePromptToken(
  item: Pick<VideoReferenceMediaIn, "kind" | "ref_id">,
  fallbackIndex = 1,
): string {
  const rawRefId = item.ref_id?.trim().toLowerCase() ?? "";
  const index = referenceRefIndex(rawRefId, item.kind);
  return `[${index ? rawRefId : referenceRefId(item.kind, fallbackIndex)}]`;
}

function referenceDisplayToken(
  item: Pick<VideoReferenceMediaIn, "kind" | "ref_id">,
  fallbackIndex = 1,
): string {
  const rawRefId = item.ref_id?.trim().toLowerCase() ?? "";
  const index = referenceRefIndex(rawRefId, item.kind) ?? fallbackIndex;
  return `@${referenceKindNoun(item.kind)}${index}`;
}

function SelectField({
  label,
  value,
  onChange,
  options,
  renderOption,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
  renderOption?: (value: string) => string;
}) {
  return (
    <label className="block min-w-0 space-y-1.5">
      {label && (
        <span className="type-caption text-[var(--fg-2)]">{label}</span>
      )}
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-11 w-full min-w-0 truncate rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-base text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60 sm:h-10 sm:text-sm"
      >
        {options.map((item) => (
          <option key={item || "auto"} value={item}>
            {renderOption ? renderOption(item) : item || "自动"}
          </option>
        ))}
      </select>
    </label>
  );
}

export function VideoParameterPanel({
  className,
  selectedModel,
  modelOptions,
  durationS,
  durationOptions,
  resolution,
  resolutionOptions,
  aspectRatio,
  aspectRatioOptions,
  seed,
  generateAudio,
  estimate,
  canSubmit,
  reason,
  loading,
  sourceReady,
  onSubmit,
  onModelChange,
  onDurationChange,
  onResolutionChange,
  onAspectRatioChange,
  onSeedChange,
  onGenerateAudioChange,
}: {
  className?: string;
  selectedModel: string;
  modelOptions: string[];
  durationS: number;
  durationOptions: string[];
  resolution: string;
  resolutionOptions: string[];
  aspectRatio: string;
  aspectRatioOptions: string[];
  seed: string;
  generateAudio: boolean;
  estimate: { tokens: number; micro: number } | null;
  canSubmit: boolean;
  reason: string;
  loading: boolean;
  sourceReady: boolean;
  onSubmit: () => void;
  onModelChange: (value: string) => void;
  onDurationChange: (value: string) => void;
  onResolutionChange: (value: string) => void;
  onAspectRatioChange: (value: string) => void;
  onSeedChange: (value: string) => void;
  onGenerateAudioChange: (value: boolean) => void;
}) {
  return (
    <aside
      id="video-generation-settings"
      className={cn(
        "flex min-w-0 flex-col overflow-hidden border-y border-[var(--border)] bg-transparent",
        "min-[1120px]:rounded-[var(--radius-panel)] min-[1120px]:border min-[1120px]:bg-[var(--bg-1)]/82 min-[1120px]:shadow-[var(--shadow-2)] min-[1120px]:backdrop-blur-xl",
        className,
      )}
    >
      <div className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border-subtle)] p-3.5">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
            <Settings2 className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <p className="type-card-title">
              视频生成参数
            </p>
            <p className="mt-0.5 truncate text-xs text-[var(--fg-2)]">
              {selectedModel || "未选择模型"}
            </p>
          </div>
        </div>
        <span
          className={cn(
            "shrink-0 whitespace-nowrap rounded-full border px-2 py-1 text-xs",
            canSubmit
              ? "border-success-border bg-success-soft text-success"
              : sourceReady
                ? "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]"
                : "border-warning-border bg-warning-soft text-[var(--warning-fg)]",
          )}
        >
          {canSubmit ? "就绪" : sourceReady ? "草稿" : "缺素材"}
        </span>
      </div>

      <div className="min-w-0 flex-1 space-y-4 p-3 sm:p-3.5">
        <section className="space-y-2.5">
          <div className="flex items-center justify-between gap-2">
            <p className="type-caption text-[var(--fg-2)]">模型</p>
            <span className="text-[11px] text-[var(--fg-2)]">
              自动匹配当前生成方式
            </span>
          </div>
          <SelectField
            label=""
            value={selectedModel}
            onChange={onModelChange}
            options={modelOptions}
          />
        </section>

        <section className="space-y-2.5">
          <p className="type-caption text-[var(--fg-2)]">画面与时长</p>
          <div className="grid min-w-0 grid-cols-1 gap-2 min-[360px]:grid-cols-2">
            <SelectField
              label="分辨率"
              value={resolution}
              onChange={onResolutionChange}
              options={resolutionOptions}
            />
            <SelectField
              label="画面比例"
              value={aspectRatio}
              onChange={onAspectRatioChange}
              options={aspectRatioOptions}
            />
          </div>
          <SelectField
            label="视频时长"
            value={String(durationS)}
            onChange={onDurationChange}
            options={durationOptions}
            renderOption={(value) => formatDurationLabel(Number(value))}
          />
        </section>

        <label className="flex min-h-12 min-w-0 cursor-pointer items-center justify-between gap-4 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/72 px-3">
          <span className="min-w-0">
            <span className="block text-sm font-medium text-[var(--fg-0)]">
              生成音频
            </span>
            <span className="mt-0.5 block text-xs text-[var(--fg-2)]">
              同步生成环境声或对白
            </span>
          </span>
          <input
            type="checkbox"
            checked={generateAudio}
            onChange={(event) => onGenerateAudioChange(event.target.checked)}
            className="peer sr-only"
          />
          <span className="relative h-6 w-10 shrink-0 rounded-full border border-[var(--border-strong)] bg-[var(--bg-2)] transition-colors peer-checked:border-[var(--accent-border)] peer-checked:bg-[var(--accent)] peer-checked:[&>span]:translate-x-4">
            <span className="absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-[var(--fg-0)] shadow-[var(--shadow-1)] transition-transform" />
          </span>
        </label>

        <details className="group overflow-hidden border-y border-[var(--border-subtle)] bg-transparent">
          <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]">
            <span>高级设置</span>
            <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
          </summary>
          <div className="border-t border-[var(--border-subtle)] p-3">
            <label className="block min-w-0 space-y-1.5">
              <span className="type-caption text-[var(--fg-2)]">Seed</span>
              <input
                value={seed}
                onChange={(event) => onSeedChange(event.target.value)}
                inputMode="numeric"
                placeholder="留空为随机"
                className="h-11 w-full min-w-0 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 font-mono text-base text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60 sm:h-10 sm:text-xs"
              />
            </label>
            <p className="mt-2 text-xs leading-5 text-[var(--fg-2)]">
              使用相同 Seed 可提高同一模型与参数下的结果可复现性。
            </p>
          </div>
        </details>
      </div>

      <div className="mt-auto shrink-0 border-t border-[var(--border)] bg-[var(--bg-1)]/72 p-3 sm:p-3.5">
        <div className="mb-3 grid grid-cols-2 gap-2 border-y border-[var(--border-subtle)] py-3">
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-2)]">预计预扣</p>
            <p className="mt-1 truncate text-lg font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? formatRmb(estimate.micro / 1_000_000) : "-"}
            </p>
          </div>
          <div className="min-w-0 border-l border-[var(--border-subtle)] pl-3">
            <p className="type-caption text-[var(--fg-2)]">Token 上限</p>
            <p className="mt-1 truncate text-sm font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? estimate.tokens.toLocaleString() : "-"}
            </p>
          </div>
        </div>
        <SubmitPanel
          canSubmit={canSubmit}
          reason={reason}
          loading={loading}
          onSubmit={onSubmit}
        />
      </div>
    </aside>
  );
}

export function VideoWorkbenchHeader({
  mode,
  profile,
  audio,
  enabled,
  loading,
  activeCount,
  historyCount,
  serviceSummary,
  submitState,
  onOpenParameters,
  onOpenTasks,
}: {
  mode: string;
  profile: string;
  audio: boolean;
  enabled: boolean;
  loading: boolean;
  activeCount: number;
  historyCount: number;
  serviceSummary: string;
  submitState: string;
  onOpenParameters: () => void;
  onOpenTasks: () => void;
}) {
  const serviceValue = loading ? "读取中" : enabled ? "在线" : "离线";

  return (
    <section className="adaptive-material sticky top-0 z-30 flex shrink-0 flex-col items-stretch gap-2 border-b border-[var(--border)] bg-[var(--bg-0)]/96 pb-3 pt-1 backdrop-blur-xl min-[390px]:flex-row min-[390px]:items-center min-[390px]:justify-between sm:gap-3">
      <div className="flex min-w-0 items-center gap-3">
        <span className="hidden h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-card)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)] sm:flex">
          <Clapperboard className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h1 className="type-page-title-sm">
              AI 视频
            </h1>
            <span
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium",
                enabled
                  ? "border-success-border bg-success-soft text-success"
                  : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-2)]",
              )}
            >
              <span
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  enabled ? "bg-[var(--success)]" : "bg-[var(--fg-3)]",
                )}
              />
              {serviceValue}
            </span>
          </div>
          <p className="mt-1 truncate text-xs text-[var(--fg-2)]">
            {loading ? "正在读取视频服务" : serviceSummary}
          </p>
        </div>
      </div>
      <div className="grid min-w-0 grid-cols-2 gap-2 min-[390px]:flex min-[390px]:flex-1 min-[390px]:items-center min-[390px]:justify-end sm:flex-none">
        <div className="hidden items-center gap-1.5 lg:flex">
          <span className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/72 px-2.5 py-1.5 text-xs text-[var(--fg-1)]">
            <Film className="h-3.5 w-3.5 text-[var(--fg-2)]" />
            {mode}
          </span>
          <span className="max-w-[160px] truncate px-1 text-xs text-[var(--fg-2)]">
            {audio ? "含音频" : "无音频"} · {submitState}
          </span>
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={onOpenParameters}
          leftIcon={<Settings2 className="h-4 w-4" />}
          className="min-h-11 shrink-0"
        >
          <span className="sm:hidden">参数</span>
          <span className="hidden sm:inline">参数 · {profile}</span>
        </Button>
        <Button
          variant={activeCount > 0 ? "secondary" : "outline"}
          size="sm"
          onClick={onOpenTasks}
          leftIcon={<ListVideo className="h-4 w-4" />}
          className="min-h-11 shrink-0"
        >
          {activeCount > 0
            ? `${activeCount} 进行中`
            : historyCount > 0
              ? `任务 ${historyCount}`
              : "任务"}
        </Button>
      </div>
    </section>
  );
}

export function ModeCard({
  actionKey,
  copy,
  selected,
  onSelect,
}: {
  actionKey: VideoAction;
  copy: ModeCardCopy;
  selected: boolean;
  onSelect: () => void;
}) {
  const icon =
    actionKey === "t2v" ? (
      <Film className="h-4 w-4" />
    ) : actionKey === "i2v" ? (
      <ImageIcon className="h-4 w-4" />
    ) : (
      <VideoIcon className="h-4 w-4" />
    );
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "group flex min-h-12 min-w-0 items-center gap-2 rounded-[var(--radius-control)] border px-2.5 py-2 text-left transition-[background-color,border-color,color,box-shadow] duration-[var(--dur-normal)] sm:px-3",
        selected
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--fg-0)]"
          : "border-transparent text-[var(--fg-1)] hover:border-[var(--border)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      <span
        className={cn(
          "hidden h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border sm:flex",
          selected
            ? "border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]"
            : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]",
        )}
      >
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs font-semibold text-[var(--fg-0)] sm:text-sm">
          {copy.title}
        </span>
        <span className="mt-0.5 hidden truncate text-[11px] text-[var(--fg-2)] md:block">
          {copy.eyebrow}
        </span>
      </span>
      {selected && (
        <CircleCheck className="h-4 w-4 shrink-0 text-[var(--accent)]" />
      )}
    </button>
  );
}

function promptEnhancePreviewCandidateId(
  candidates: PromptEnhanceCandidate[],
  previewCandidateId: string,
  selectedId: string,
): string {
  if (candidates.some((candidate) => candidate.id === previewCandidateId)) {
    return previewCandidateId;
  }
  if (candidates.some((candidate) => candidate.id === selectedId)) {
    return selectedId;
  }
  return candidates[0]?.id ?? "";
}

function promptEnhanceChooserSubtitle({
  loading,
  candidateCount,
  autoApplied,
}: {
  loading: boolean;
  candidateCount: number;
  autoApplied: boolean;
}): string {
  if (candidateCount > 1) {
    return autoApplied
      ? `${candidateCount} 个候选，已应用推荐版`
      : `${candidateCount} 个候选，未自动替换`;
  }
  if (loading) return "按火山视频结构补动作、运镜和参考一致性";
  return autoApplied ? "已应用到描述" : "已保留原描述";
}

function promptEnhanceActionLabel(action: PromptEnhanceAction): string {
  if (action === "light_refine") return "轻度优化";
  if (action === "direct_pass") return "直接优化";
  if (action === "ask_first") return "需要补充";
  if (action === "keep_original") return "建议保留原稿";
  if (action === "optional_vc") return "可选改写";
  return "完整改写";
}

function PromptEnhanceLoadingState({ preview }: { preview: string }) {
  return (
    <div className="space-y-3 p-3 sm:p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-[var(--fg-0)]">
            正在生成可比较版本
          </p>
          <p className="mt-0.5 text-xs text-[var(--fg-2)]">
            完成后可逐个预览，不会直接覆盖当前描述。
          </p>
        </div>
        <span className="shrink-0 rounded-full border border-[var(--accent-border)] bg-[var(--accent-soft)] px-2 py-1 text-[10px] font-medium text-[var(--accent)]">
          AI 整理中
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <div className="h-full w-1/2 animate-pulse rounded-full bg-[var(--accent)]" />
      </div>
      <div
        role="status"
        aria-live="polite"
        className="min-h-36 whitespace-pre-wrap break-words rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72 p-3 text-sm leading-7 text-[var(--fg-1)]"
      >
        {preview || "等待模型返回优化方案..."}
      </div>
    </div>
  );
}

function PromptEnhanceCandidateCard({
  candidate,
  index,
  selected,
  previewing,
  onPreview,
}: {
  candidate: PromptEnhanceCandidate;
  index: number;
  selected: boolean;
  previewing: boolean;
  onPreview: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={previewing}
      onClick={onPreview}
      className={cn(
        "flex min-h-32 w-[min(82vw,20rem)] shrink-0 flex-col rounded-[var(--radius-control)] border p-3 text-left transition-[background-color,border-color,box-shadow] lg:w-auto lg:min-w-0",
        selected
          ? "border-success-border bg-success-soft shadow-[var(--shadow-1)]"
          : previewing
            ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
            : "border-[var(--border-subtle)] bg-[var(--bg-0)]/72 hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]",
      )}
    >
      <span className="flex min-w-0 items-center gap-2">
        <span
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] border font-mono text-[10px]",
            selected
              ? "border-success-border text-success"
              : previewing
                ? "border-[var(--accent-border)] text-[var(--accent)]"
                : "border-[var(--border)] text-[var(--fg-2)]",
          )}
        >
          {String(index + 1).padStart(2, "0")}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="min-w-0 flex-1 truncate text-sm font-semibold text-[var(--fg-0)]">
              {candidate.title}
            </span>
            {index === 0 && (
              <span className="shrink-0 rounded-full border border-[var(--accent-border)] bg-[var(--bg-0)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--accent)]">
                推荐
              </span>
            )}
          </span>
          <span className="mt-0.5 block text-[11px] text-[var(--fg-2)]">
            {promptEnhanceActionLabel(candidate.action)}
          </span>
        </span>
        {selected && <CircleCheck className="h-4 w-4 shrink-0 text-success" />}
      </span>
      <span className="mt-2 line-clamp-2 text-xs leading-5 text-[var(--fg-1)]">
        {candidate.prompt}
      </span>
      <span className="mt-auto flex items-center justify-between gap-2 pt-2 text-[10px] text-[var(--fg-2)]">
        <span>{candidate.prompt.length.toLocaleString()} 字</span>
        <span
          className={
            selected ? "text-success" : previewing ? "text-[var(--accent)]" : ""
          }
        >
          {selected ? "已应用" : previewing ? "正在预览" : "查看方案"}
        </span>
      </span>
    </button>
  );
}

function PromptEnhanceCandidatePreview({
  candidate,
  selected,
  onApply,
  onCopy,
}: {
  candidate: PromptEnhanceCandidate;
  selected: boolean;
  onApply: () => void;
  onCopy: () => void;
}) {
  const applicable = canApplyPromptEnhanceCandidate(candidate);
  return (
    <article className="overflow-hidden rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/68 px-3 py-2.5 sm:px-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-[var(--fg-0)]">
              {candidate.title}
            </h3>
            <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-0.5 text-[10px] text-[var(--fg-2)]">
              {promptEnhanceActionLabel(candidate.action)}
            </span>
            {selected && (
              <span className="rounded-full border border-success-border bg-success-soft px-2 py-0.5 text-[10px] font-medium text-success">
                当前已应用
              </span>
            )}
          </div>
          <p className="mt-1 text-xs text-[var(--fg-2)]">
            {applicable
              ? "先完整预览，再决定是否替换编辑器中的描述。"
              : "这是 AI 的判断与补充建议，仅供查看。"}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button
            variant={selected ? "secondary" : "primary"}
            size="sm"
            disabled={selected || !applicable}
            onClick={onApply}
          >
            {promptEnhanceCandidateButtonText(candidate, selected)}
          </Button>
          <IconButton
            variant="ghost"
            size="sm"
            onClick={onCopy}
            aria-label="复制优化提示词"
            tooltip="复制提示词"
          >
            <Copy className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      </header>
      <div className="whitespace-pre-wrap break-words px-3 py-3 text-sm leading-7 text-[var(--fg-1)] sm:px-4 sm:py-4">
        {candidate.prompt}
      </div>
      <footer className="border-t border-[var(--border-subtle)] px-3 py-2 text-[10px] tabular-nums text-[var(--fg-2)] sm:px-4">
        完整提示词 · {candidate.prompt.length.toLocaleString()} 字
      </footer>
    </article>
  );
}

export function PromptEnhanceChooser({
  loading,
  preview,
  candidates,
  selectedId,
  onSelect,
  onDismiss,
  onReturnToEditor,
}: {
  loading: boolean;
  preview: string;
  candidates: PromptEnhanceCandidate[];
  selectedId: string;
  onSelect: (candidate: PromptEnhanceCandidate) => void;
  onDismiss: () => void;
  onReturnToEditor: () => void;
}) {
  const cleanPreview = cleanPromptEnhanceText(preview);
  const visibleCandidates = candidates;
  const firstCandidate = visibleCandidates[0];
  const [previewCandidateId, setPreviewCandidateId] = useState("");
  const effectivePreviewCandidateId = promptEnhancePreviewCandidateId(
    visibleCandidates,
    previewCandidateId,
    selectedId,
  );
  const previewCandidate =
    visibleCandidates.find(
      (candidate) => candidate.id === effectivePreviewCandidateId,
    ) ??
    firstCandidate ??
    null;
  const autoApplied =
    firstCandidate != null &&
    firstCandidate.id === selectedId &&
    shouldAutoApplyPromptEnhanceCandidate(firstCandidate);

  const copyCandidate = async (candidate: PromptEnhanceCandidate) => {
    try {
      await navigator.clipboard.writeText(candidate.prompt);
      toast.success("已复制提示词");
    } catch {
      toast.error("复制失败");
    }
  };

  return (
    <section className="overflow-hidden border-y border-[var(--border)] bg-transparent">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3 py-2.5 sm:px-4">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]">
            {loading ? (
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Sparkles className="h-3.5 w-3.5" />
            )}
          </span>
          <span className="min-w-0">
            <span className="block text-sm font-semibold text-[var(--fg-0)]">
              {loading ? "正在优化提示词" : "AI 优化结果"}
            </span>
            <span className="block truncate text-xs text-[var(--fg-2)]">
              {promptEnhanceChooserSubtitle({
                loading,
                candidateCount: visibleCandidates.length,
                autoApplied,
              })}
            </span>
          </span>
        </div>
        {!loading && (
          <div className="flex shrink-0 items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={onReturnToEditor}
              leftIcon={<PencilLine className="h-3.5 w-3.5" />}
            >
              回到编辑
            </Button>
            <IconButton
              variant="ghost"
              size="sm"
              onClick={onDismiss}
              aria-label="关闭优化结果"
              tooltip="关闭优化结果"
            >
              <X className="h-4 w-4" />
            </IconButton>
          </div>
        )}
      </header>

      {loading && <PromptEnhanceLoadingState preview={cleanPreview} />}

      {!loading && visibleCandidates.length > 0 && previewCandidate && (
        <div className="space-y-3 p-3 sm:p-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs font-medium text-[var(--fg-1)]">
              选择一个优化方向
            </p>
            <p className="text-[10px] text-[var(--fg-2)]">
              点击卡片切换完整预览
            </p>
          </div>
          <div className="flex gap-2 overflow-x-auto pb-1 lg:grid lg:grid-cols-3 lg:overflow-visible">
            {visibleCandidates.map((candidate, index) => (
              <PromptEnhanceCandidateCard
                key={candidate.id}
                candidate={candidate}
                index={index}
                selected={candidate.id === selectedId}
                previewing={candidate.id === previewCandidate.id}
                onPreview={() => setPreviewCandidateId(candidate.id)}
              />
            ))}
          </div>
          <PromptEnhanceCandidatePreview
            candidate={previewCandidate}
            selected={previewCandidate.id === selectedId}
            onApply={() => onSelect(previewCandidate)}
            onCopy={() => void copyCandidate(previewCandidate)}
          />
        </div>
      )}
    </section>
  );
}

export function ReferenceChip({
  item,
  active,
  onInsert,
  onPreview,
  onRemove,
}: {
  item: ReferenceDraft;
  active: boolean;
  onInsert: () => void;
  onPreview: () => void;
  onRemove: () => void;
}) {
  const displayToken = referenceDisplayToken(item);
  const anchorToken = referencePromptToken(item);
  return (
    <div
      className={cn(
        "relative flex h-24 w-[min(82vw,19rem)] max-w-[calc(100vw-3rem)] shrink-0 overflow-hidden rounded-[var(--radius-control)] border bg-[var(--bg-1)] text-xs text-[var(--fg-1)] transition-[background-color,border-color,box-shadow]",
        active
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
          : "border-[var(--border)]",
      )}
    >
      <button
        type="button"
        onClick={onPreview}
        title={`查看 ${displayToken} 预览`}
        aria-label={`查看 ${displayToken} 预览`}
        className="shrink-0 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/50"
      >
        <ReferenceThumbnail item={item} active={active} />
      </button>
      <button
        type="button"
        onClick={onInsert}
        title={
          active
            ? `已引用 ${displayToken}，提交时映射为 ${anchorToken}`
            : `插入 ${displayToken}`
        }
        className="flex min-w-0 flex-1 cursor-pointer flex-col justify-center gap-1 px-3 py-2.5 pr-9 text-left transition-colors hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/50"
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 font-semibold text-[var(--fg-0)]">
            {displayToken}
          </span>
          <span className="min-w-0 truncate text-[var(--fg-2)]">
            {item.label}
          </span>
        </span>
        <span className="max-w-full truncate font-mono text-[11px] text-[var(--fg-2)]">
          {item.display}
        </span>
        <span className="text-[11px] text-[var(--fg-2)]">
          {active ? "已用于提示词" : "点击文字插入引用"}
        </span>
      </button>
      <button
        type="button"
        aria-label="移除参考素材"
        onClick={onRemove}
        className="absolute right-0 top-0 flex h-11 w-11 shrink-0 items-start justify-end rounded-bl-[var(--radius-control)] bg-[var(--bg-1)]/85 p-2 text-[var(--fg-2)] shadow-[var(--shadow-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]"
      >
        <XCircle className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function ReferenceThumbnail({
  item,
  active,
}: {
  item: ReferenceDraft;
  active: boolean;
}) {
  const previewUrl = cleanReferencePreviewUrl(item.previewUrl);
  const [failedPreviewUrl, setFailedPreviewUrl] = useState<string | null>(null);
  const failed = previewUrl != null && failedPreviewUrl === previewUrl;
  const showPreview = Boolean(previewUrl && !failed);
  const Icon = item.kind === "video" ? VideoIcon : item.url ? Tags : ImageIcon;

  return (
    <span className="relative flex h-24 w-32 shrink-0 overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-0)] text-[var(--fg-2)]">
      {showPreview ? (
        <img
          src={previewUrl ?? ""}
          alt=""
          className="h-full w-full object-cover"
          loading="lazy"
          decoding="async"
          onError={() => setFailedPreviewUrl(previewUrl)}
        />
      ) : (
        <span className="flex h-full w-full flex-col items-center justify-center gap-1 px-2 text-center">
          <Icon className="h-5 w-5" aria-hidden="true" />
          <span className="text-[10px] font-medium leading-3">
            {failed ? "预览失败" : "暂无预览"}
          </span>
        </span>
      )}
      {showPreview && (
        <span className="absolute bottom-1.5 left-1.5 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-0)]/82 p-1 text-[var(--fg-1)] shadow-[var(--shadow-1)]">
          <Maximize2 className="h-3 w-3" aria-hidden="true" />
        </span>
      )}
      {active && (
        <span className="absolute right-1.5 top-1.5 rounded-full border border-[var(--bg-1)] bg-[var(--accent)] p-0.5 text-[var(--accent-on)] shadow-[var(--shadow-1)]">
          <CircleCheck className="h-2.5 w-2.5" aria-hidden="true" />
        </span>
      )}
      {item.kind === "video" && showPreview && (
        <span className="absolute bottom-1.5 right-1.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/85 p-0.5 text-[var(--fg-1)]">
          <VideoIcon className="h-2.5 w-2.5" aria-hidden="true" />
        </span>
      )}
    </span>
  );
}

function referenceMediaPreviewSources(item: ReferenceDraft): {
  mediaUrl: string | null;
  posterUrl: string | null;
} {
  const previewUrl = cleanReferencePreviewUrl(item.previewUrl);
  if (item.kind === "image") {
    return { mediaUrl: previewUrl, posterUrl: null };
  }

  const directMediaUrl = cleanReferencePreviewUrl(item.url);
  if (item.kind === "audio") {
    return {
      mediaUrl: directMediaUrl ?? previewUrl,
      posterUrl: null,
    };
  }

  const videoId = item.video_id?.trim();
  const mediaUrl = videoId
    ? videoBinaryUrl(videoId)
    : (directMediaUrl ?? previewUrl);
  return {
    mediaUrl,
    posterUrl:
      previewUrl && previewUrl !== mediaUrl ? previewUrl : null,
  };
}

function ReferenceMediaPreviewIcon({ item }: { item: ReferenceDraft }) {
  if (item.kind === "video") {
    return <VideoIcon className="h-8 w-8" aria-hidden="true" />;
  }
  if (item.kind === "audio") {
    return <AudioLines className="h-8 w-8" aria-hidden="true" />;
  }
  if (item.url) {
    return <Tags className="h-8 w-8" aria-hidden="true" />;
  }
  return <ImageIcon className="h-8 w-8" aria-hidden="true" />;
}

function ReferenceMediaPreviewContent({
  item,
  displayToken,
  mediaUrl,
  posterUrl,
  failed,
  onError,
}: {
  item: ReferenceDraft;
  displayToken: string;
  mediaUrl: string | null;
  posterUrl: string | null;
  failed: boolean;
  onError: () => void;
}) {
  const referenceNoun = referenceKindNoun(item.kind);

  if (!mediaUrl || failed) {
    return (
      <div
        role={failed ? "alert" : "status"}
        className="flex flex-col items-center justify-center gap-2 px-5 text-center text-[var(--fg-2)]"
      >
        <ReferenceMediaPreviewIcon item={item} />
        <p className="text-sm font-medium text-[var(--fg-1)]">
          {failed
            ? `${referenceNoun}预览加载失败`
            : `这个${referenceNoun}暂无可显示预览`}
        </p>
        <p className="max-w-md text-xs leading-5">
          {failed
            ? "请确认素材仍可访问，或稍后重试。"
            : `官方${referenceNoun}素材可能只有素材 ID，暂时无法在这里直接预览。`}
        </p>
      </div>
    );
  }

  if (item.kind === "video") {
    return (
      <video
        src={mediaUrl}
        poster={posterUrl ?? undefined}
        controls
        playsInline
        preload="metadata"
        aria-label={`${displayToken} 视频预览`}
        className="h-full w-full object-contain"
        onError={onError}
      >
        当前浏览器不支持视频预览。
      </video>
    );
  }

  if (item.kind === "audio") {
    return (
      <div className="flex w-full max-w-2xl flex-col items-center gap-4 px-5 py-8">
        <AudioLines
          className="h-8 w-8 text-[var(--fg-2)]"
          aria-hidden="true"
        />
        <audio
          src={mediaUrl}
          controls
          preload="metadata"
          aria-label={`${displayToken} 音频预览`}
          className="w-full"
          onError={onError}
        >
          当前浏览器不支持音频预览。
        </audio>
      </div>
    );
  }

  return (
    <img
      src={mediaUrl}
      alt={`${displayToken} 图片预览`}
      className="h-full w-full object-contain"
      decoding="async"
      onError={onError}
    />
  );
}

export function ReferenceMediaPreviewDialog({
  item,
  onClose,
  onInsert,
}: {
  item: ReferenceDraft;
  onClose: () => void;
  onInsert: () => void;
}) {
  const dialogRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);
  const { mediaUrl, posterUrl } = referenceMediaPreviewSources(item);
  const [failedPreviewUrl, setFailedPreviewUrl] = useState<string | null>(null);
  const failed = mediaUrl != null && failedPreviewUrl === mediaUrl;
  const displayToken = referenceDisplayToken(item);
  const referenceNoun = referenceKindNoun(item.kind);

  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const dialog = dialogRef.current;
    const focusFrame = window.requestAnimationFrame(() => {
      focusVideoWorkbenchElement(dialog, { preventScroll: true });
    });
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isTopmostVideoDialog(dialog)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      trapVideoDialogFocus(event, dialog);
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("keydown", handleKeyDown);
      restoreVideoWorkbenchFocus(previousFocus, dialog);
    };
  }, []);

  return (
    <div
      className="mobile-dialog-shell mobile-perf-surface fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/70 backdrop-blur-md sm:items-center sm:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`reference-preview-${item._key}`}
        aria-describedby={`reference-preview-description-${item._key}`}
        tabIndex={-1}
        className="mobile-dialog-panel flex h-[var(--mobile-dialog-max-height)] w-full max-w-4xl flex-col overflow-hidden rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] sm:h-[min(760px,calc(100dvh-2.5rem))] sm:rounded-[var(--radius-panel)] sm:border-b landscape:max-sm:rounded-[var(--radius-panel)] landscape:max-sm:border-b"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-1)]/95 px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-2)]">
              {`参考${referenceNoun}`}
            </p>
            <h2
              id={`reference-preview-${item._key}`}
              className="mt-1 truncate text-base font-semibold text-[var(--fg-0)]"
            >
              {displayToken} · {item.label}
            </h2>
            <p
              id={`reference-preview-description-${item._key}`}
              className="mt-1 truncate font-mono text-xs text-[var(--fg-2)]"
            >
              {item.display}
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-11 w-11 shrink-0 px-0"
            onClick={onClose}
            aria-label="关闭参考素材预览"
          >
            <XCircle className="h-4 w-4" />
          </Button>
        </header>
        <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto bg-[var(--bg-0)] p-3 sm:p-5">
          <div className="flex h-full min-h-0 items-center justify-center overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] sm:min-h-[18rem]">
            <ReferenceMediaPreviewContent
              item={item}
              displayToken={displayToken}
              mediaUrl={mediaUrl}
              posterUrl={posterUrl}
              failed={failed}
              onError={() => setFailedPreviewUrl(mediaUrl)}
            />
          </div>
        </div>
        <footer className="mobile-dialog-footer flex shrink-0 flex-col items-stretch gap-2 border-t border-[var(--border)] bg-[var(--bg-1)]/88 px-4 py-3 min-[390px]:flex-row min-[390px]:items-center min-[390px]:justify-between sm:px-5">
          <span className="truncate text-xs text-[var(--fg-2)]">
            提交时映射为 {referencePromptToken(item)}
          </span>
          <div className="grid shrink-0 grid-cols-2 gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>
              关闭
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={onInsert}
              leftIcon={<Tags className="h-3.5 w-3.5" />}
            >
              插入引用
            </Button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function SubmitPanel({
  canSubmit,
  reason,
  loading,
  onSubmit,
}: {
  canSubmit: boolean;
  reason: string;
  loading: boolean;
  onSubmit: () => void;
}) {
  return (
    <div className="space-y-2">
      <p
        className={cn(
          "flex min-w-0 items-center gap-2 text-xs leading-5",
          canSubmit ? "text-success" : "text-[var(--fg-2)]",
        )}
      >
        <span
          className={cn(
            "h-1.5 w-1.5 shrink-0 rounded-full",
            canSubmit ? "bg-[var(--success)]" : "bg-[var(--fg-3)]",
          )}
        />
        <span className="truncate">{reason}</span>
      </p>
      <Button
        variant="primary"
        size="lg"
        fullWidth
        disabled={!canSubmit}
        loading={loading}
        onClick={onSubmit}
        leftIcon={<Send className="h-4 w-4" />}
      >
        生成视频
      </Button>
    </div>
  );
}
