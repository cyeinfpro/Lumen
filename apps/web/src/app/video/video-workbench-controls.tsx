"use client";

import type { ReactNode } from "react";
import {
  ChevronDown,
  CircleCheck,
  Clapperboard,
  Film,
  ImageIcon,
  ListVideo,
  Send,
  Settings2,
  Video as VideoIcon,
} from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { formatRmb } from "@/lib/money";
import type { VideoAction } from "@/lib/types";
import { cn } from "@/lib/utils";

const SMART_VIDEO_DURATION = -1;

type ModeCardCopy = {
  title: string;
  eyebrow: string;
};

export type VideoParameterPanelProps = {
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
};

type VideoParameterPanelViewProps = VideoParameterPanelProps & {
  id: string;
  children: ReactNode;
};

function formatDurationLabel(durationS: number): string {
  return durationS === SMART_VIDEO_DURATION ? "自动时长" : `${durationS}s`;
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
        className="h-11 w-full min-w-0 truncate rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 type-body text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60 sm:h-10 "
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
          "flex min-w-0 items-center gap-2 type-caption leading-5",
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

export function VideoParameterPanelView({
  id,
  children,
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
}: VideoParameterPanelViewProps) {
  return (
    <aside
      id={id}
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
            <p className="type-card-title">{children}</p>
            <p className="mt-0.5 truncate type-caption text-[var(--fg-2)]">
              {selectedModel || "未选择模型"}
            </p>
          </div>
        </div>
        <span
          className={cn(
            "shrink-0 whitespace-nowrap rounded-full border px-2 py-1 type-caption",
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
            <span className="type-caption text-[var(--fg-2)]">
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
            <span className="block type-body-sm font-medium text-[var(--fg-0)]">
              生成音频
            </span>
            <span className="mt-0.5 block type-caption text-[var(--fg-2)]">
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
          <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 type-caption font-medium text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]">
            <span>高级设置</span>
            <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
          </summary>
          <div className="border-t border-[var(--border-subtle)] p-3">
            <label className="block min-w-0 space-y-1.5">
              <span className="type-caption text-[var(--fg-2)]">种子</span>
              <input
                value={seed}
                onChange={(event) => onSeedChange(event.target.value)}
                inputMode="numeric"
                placeholder="留空为随机"
                className="h-11 w-full min-w-0 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 font-mono type-body text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60 sm:h-10 "
              />
            </label>
            <p className="mt-2 type-caption leading-5 text-[var(--fg-2)]">
              使用相同种子可提高同一模型与参数下的结果可复现性。
            </p>
          </div>
        </details>
      </div>

      <div className="mt-auto shrink-0 border-t border-[var(--border)] bg-[var(--bg-1)]/72 p-3 sm:p-3.5">
        <div className="mb-3 grid grid-cols-2 gap-2 border-y border-[var(--border-subtle)] py-3">
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-2)]">预计预扣</p>
            <p className="mt-1 truncate type-card-title font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? formatRmb(estimate.micro / 1_000_000) : "-"}
            </p>
          </div>
          <div className="min-w-0 border-l border-[var(--border-subtle)] pl-3">
            <p className="type-caption text-[var(--fg-2)]">Token 上限</p>
            <p className="mt-1 truncate type-body-sm font-semibold tabular-nums text-[var(--fg-0)]">
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
    <section className="sticky top-0 z-30 flex shrink-0 flex-col items-stretch gap-2 border-b border-[var(--border)] bg-[var(--bg-0)]/96 pb-3 pt-1 backdrop-blur-xl min-[390px]:flex-row min-[390px]:items-center min-[390px]:justify-between sm:gap-3">
      <div className="flex min-w-0 items-center gap-3">
        <span className="hidden h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-card)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)] sm:flex">
          <Clapperboard className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h1 className="type-page-title-sm">AI 视频</h1>
            <span
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 type-caption font-medium",
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
          <p className="mt-1 truncate type-caption text-[var(--fg-2)]">
            {loading ? "视频服务读取中" : serviceSummary}
          </p>
        </div>
      </div>
      <div className="grid min-w-0 grid-cols-2 gap-2 min-[390px]:flex min-[390px]:flex-1 min-[390px]:items-center min-[390px]:justify-end sm:flex-none">
        <div className="hidden items-center gap-1.5 lg:flex">
          <span className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/72 px-2.5 py-1.5 type-caption text-[var(--fg-1)]">
            <Film className="h-3.5 w-3.5 text-[var(--fg-2)]" />
            {mode}
          </span>
          <span className="max-w-[160px] truncate px-1 type-caption text-[var(--fg-2)]">
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
        <span className="block truncate type-body-sm font-semibold text-[var(--fg-0)] ">
          {copy.title}
        </span>
        <span className="mt-0.5 hidden truncate type-caption text-[var(--fg-2)] md:block">
          {copy.eyebrow}
        </span>
      </span>
      {selected && (
        <CircleCheck className="h-4 w-4 shrink-0 text-[var(--accent)]" />
      )}
    </button>
  );
}
