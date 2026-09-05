"use client";

import { useRef, useState } from "react";
import {
  ChevronDown,
  ImageIcon,
  Layers3,
  RefreshCw,
  Send,
  Settings2,
  Tags,
  Upload,
  Video as VideoIcon,
} from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { formatMicroRmb } from "./video-workbench-controls";
import { DesktopTopNav, MobileTabBar } from "@/components/ui/shell";
import type {
  VideoAction,
  VideoGenerationOut,
} from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  ModeCard,
  ReferenceChip,
  ReferenceMediaPreviewDialog,
  VideoParameterPanel,
  VideoWorkbenchHeader,
} from "./video-workbench-ui";
import type { ReferenceDraft } from "./video-workbench-ui";
import {
  MODE_COPY,
  actionLabel,
} from "./video-task-model";
import type {
  VideoGenerationWithVideo,
  VideoHistoryFilter,
} from "./video-task-model";
import {
  VideoPreviewDialog,
  VideoTaskDrawer,
} from "./video-task-ui";
import type {
  NormalizedVideoImageConstraints,
  VideoEstimate,
} from "./video-options-model";
import {
  promptContainsReferenceMention,
  referenceKindNoun,
} from "./video-reference-domain";
import type {
  ReferenceKind,
  ReferenceLimits,
  VolcanoAssetReferenceCandidate,
} from "./video-reference-domain";
import { VolcanoAssetManager } from "./volcano-asset-manager";
import { VideoDirectorViewport } from "./video-director-viewport";
import {
  VideoPromptEditor,
  type VideoPromptEditorModel,
} from "./video-prompt-editor";

export type VideoPageViewModel = {
  viewport: {
    item: VideoGenerationWithVideo | null;
    loading: boolean;
    error?: string | null;
    onRetry: () => void;
    onPreview: (item: VideoGenerationWithVideo) => void;
  };
  header: {
    action: VideoAction;
    parameterProfile: string;
    generateAudio: boolean;
    serviceEnabled: boolean;
    optionsLoading: boolean;
    activeCount: number;
    historyCount: number;
    serviceSummary: string;
    submitDisabledReason: string;
    onOpenParameters: () => void;
    onOpenTasks: () => void;
  };
  composer: {
    action: VideoAction;
    actionOptions: VideoAction[];
    onActionChange: (action: VideoAction) => void;
    firstFrame: {
      pending: boolean;
      inputImageId: string;
      uploadedLabel: string;
      imageConstraints: NormalizedVideoImageConstraints | null;
      onFile: (file: File) => void;
      onInputImageIdChange: (value: string) => void;
    };
    references: {
      pending: boolean;
      counts: ReferenceLimits;
      limits: ReferenceLimits;
      total: number;
      totalLimit: number | null;
      imageConstraints: NormalizedVideoImageConstraints | null;
      items: ReferenceDraft[];
      prompt: string;
      kindOptions: ReferenceKind[];
      selectedKind: ReferenceKind;
      assetUrlInput: string;
      onFiles: (files: File[]) => void;
      onOpenAssetManager: () => void;
      onInsert: (item: ReferenceDraft) => void;
      onPreview: (item: ReferenceDraft) => void;
      onRemove: (item: ReferenceDraft) => void;
      onKindChange: (kind: ReferenceKind) => void;
      onAssetUrlInputChange: (value: string) => void;
      onAddAssetReference: () => void;
    };
    prompt: VideoPromptEditorModel;
  };
  parameters: {
    selectedModel: string;
    modelOptions: string[];
    modelOptionLabels: Record<string, string>;
    durationS: number;
    durationOptions: string[];
    resolution: string;
    resolutionOptions: string[];
    aspectRatio: string;
    aspectRatioOptions: string[];
    seed: string;
    generateAudio: boolean;
    audioSupported: boolean;
    estimate: VideoEstimate | null;
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
  assetManager: {
    open: boolean;
    model: string;
    remainingLimits: Pick<ReferenceLimits, "image" | "video">;
    existingAssetIds: Set<string>;
    onClose: () => void;
    onUse: (assets: VolcanoAssetReferenceCandidate[]) => void;
    onDeleted: (assetIds: string[]) => void;
  };
  tasks: {
    open: boolean;
    activeItems: VideoGenerationOut[];
    historyItems: VideoGenerationOut[];
    historyFilter: VideoHistoryFilter;
    historyCounts: Record<VideoHistoryFilter, number>;
    historyLoading: boolean;
    historyError?: string | null;
    cancelPendingId?: string;
    historyHasNextPage: boolean;
    historyFetchingNextPage: boolean;
    retryDisabled: boolean;
    selectedVideoId: string;
    onClose: () => void;
    onHistoryFilterChange: (value: VideoHistoryFilter) => void;
    onRefresh: () => void;
    onLoadMore: () => void;
    onCancel: (item: VideoGenerationOut) => void;
    onRetry: (item: VideoGenerationOut) => void;
    onCopy: (item: VideoGenerationOut) => void;
    onUseDraft: (item: VideoGenerationOut) => void;
    onDelete: (item: VideoGenerationOut) => void;
    onPreview: (item: VideoGenerationOut) => void;
  };
  playback: {
    item: VideoGenerationWithVideo | undefined;
    onClose: () => void;
    onUseDraft: () => void;
    onRetry: () => void;
    onCopy: () => void;
    onDelete: () => void;
  };
  referencePreview: {
    item: ReferenceDraft | null;
    onClose: () => void;
    onInsert: () => void;
  };
};

function ModeSelector({
  action,
  actionOptions,
  onActionChange,
}: {
  action: VideoAction;
  actionOptions: VideoAction[];
  onActionChange: (action: VideoAction) => void;
}) {
  return (
    <div className="shrink-0 border-b border-[var(--border-subtle)] p-2.5 sm:p-3">
      <div className="mb-2 flex flex-wrap items-end justify-between gap-2 px-1">
        <div>
          <p className="type-body-sm font-semibold text-[var(--fg-0)]">生成方式</p>
          <p className="mt-0.5 type-caption text-[var(--fg-2)]">
            {MODE_COPY[action].description}
          </p>
        </div>
        <span className="type-caption font-medium text-[var(--fg-1)]">
          {MODE_COPY[action].requirement}
        </span>
      </div>
      <div
        className="grid min-w-0 gap-1 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/74 p-1"
        style={{
          gridTemplateColumns: `repeat(${Math.max(actionOptions.length, 1)}, minmax(0, 1fr))`,
        }}
      >
        {actionOptions.map((key) => (
          <ModeCard
            key={key}
            actionKey={key}
            copy={MODE_COPY[key]}
            selected={action === key}
            onSelect={() => onActionChange(key)}
          />
        ))}
        {actionOptions.length === 0 && (
          <span className="px-3 py-2 text-center type-caption text-[var(--fg-2)]">
            暂无可用生成方式
          </span>
        )}
      </div>
    </div>
  );
}

function imageConstraintSummary(
  constraints: NormalizedVideoImageConstraints | null,
): string | null {
  if (!constraints) return null;
  const parts: string[] = [];
  const minSide = Math.max(
    constraints.minWidthPx ?? 0,
    constraints.minHeightPx ?? 0,
  );
  const maxSide = Math.min(
    constraints.maxWidthPx ?? Number.POSITIVE_INFINITY,
    constraints.maxHeightPx ?? Number.POSITIVE_INFINITY,
  );
  if (minSide > 0 && Number.isFinite(maxSide)) {
    parts.push(`${minSide}-${maxSide}px`);
  } else if (minSide > 0) {
    parts.push(`至少 ${minSide}px`);
  } else if (Number.isFinite(maxSide)) {
    parts.push(`不超过 ${maxSide}px`);
  }
  if (constraints.maxBytes) {
    const megabytes = constraints.maxBytes / 1024 / 1024;
    parts.push(
      `< ${Number.isInteger(megabytes) ? megabytes : megabytes.toFixed(1)} MB`,
    );
  }
  return parts.length > 0 ? parts.join(" · ") : null;
}

function imageAcceptValue(
  constraints: NormalizedVideoImageConstraints | null,
): string {
  return constraints?.mimeTypes.length
    ? constraints.mimeTypes.join(",")
    : "image/png,image/jpeg,image/webp,image/mpo";
}

function FirstFrameSection({
  model,
}: {
  model: VideoPageViewModel["composer"]["firstFrame"];
}) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const constraintSummary = imageConstraintSummary(model.imageConstraints);
  return (
    <section className="surface-section overflow-hidden">
      <input
        ref={fileInputRef}
        type="file"
        accept={imageAcceptValue(model.imageConstraints)}
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) model.onFile(file);
          event.target.value = "";
        }}
      />
      <div className="flex flex-col items-start gap-1.5 border-b border-[var(--border-subtle)] px-3 py-2.5 min-[390px]:flex-row min-[390px]:items-center min-[390px]:justify-between">
        <div className="flex items-center gap-2">
          <ImageIcon className="h-4 w-4 text-[var(--accent)]" />
          <p className="type-body-sm font-semibold text-[var(--fg-0)]">首帧素材</p>
        </div>
        <span className="type-caption text-[var(--fg-2)]">
          {constraintSummary || "用图片锁定构图与起始状态"}
        </span>
      </div>
      <div className="grid gap-3 p-3 lg:grid-cols-[minmax(0,1fr)_minmax(220px,0.42fr)] lg:items-end">
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={model.pending}
          className="group flex min-h-16 items-center gap-3 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-1)]/72 p-3 text-left transition-[background-color,border-color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] disabled:pointer-events-none disabled:opacity-60"
        >
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
            {model.pending ? (
              <RefreshCw className="h-4 w-4 animate-spin" />
            ) : (
              <Upload className="h-4 w-4" />
            )}
          </span>
          <span className="min-w-0">
            <span className="block type-body-sm font-semibold text-[var(--fg-0)]">
              {model.inputImageId ? "替换首帧" : "上传首帧图片"}
            </span>
            <span className="mt-1 block truncate type-caption text-[var(--fg-2)]">
              {model.uploadedLabel || model.inputImageId
                ? model.uploadedLabel || "已填写图片 ID"
                : constraintSummary || "PNG、JPEG、WEBP"}
            </span>
          </span>
        </button>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">
            或粘贴图片 ID
          </span>
          <input
            value={model.inputImageId}
            onChange={(event) => model.onInputImageIdChange(event.target.value)}
            placeholder="image_id"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 font-mono type-caption text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60"
          />
        </label>
      </div>
    </section>
  );
}

function ReferenceSection({
  model,
}: {
  model: VideoPageViewModel["composer"]["references"];
}) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const constraintSummary = imageConstraintSummary(model.imageConstraints);
  return (
    <section className="surface-section overflow-hidden">
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={`${imageAcceptValue(model.imageConstraints)},video/mp4,video/quicktime`}
        className="hidden"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          if (files.length > 0) model.onFiles(files);
          event.target.value = "";
        }}
      />
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3 py-2.5">
        <div className="flex items-center gap-2">
          <VideoIcon className="h-4 w-4 text-[var(--accent)]" />
          <p className="type-body-sm font-semibold text-[var(--fg-0)]">参考素材</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 type-caption text-[var(--fg-2)]">
          <span>
            图片 {model.counts.image}/{model.limits.image}
          </span>
          <span>
            视频 {model.counts.video}/{model.limits.video}
          </span>
          <span>
            音频 {model.counts.audio}/{model.limits.audio}
          </span>
          {model.totalLimit !== null && (
            <span>
              总计 {model.total}/{model.totalLimit}
            </span>
          )}
        </div>
      </div>
      <div className="space-y-3 p-3">
        <div className="flex min-w-0 flex-col items-stretch gap-2 min-[390px]:flex-row min-[390px]:items-center">
          <Button
            variant="outline"
            size="sm"
            loading={model.pending}
            disabled={model.pending}
            onClick={() => fileInputRef.current?.click()}
            leftIcon={<Upload className="h-3.5 w-3.5" />}
          >
            上传参考
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={model.pending}
            onClick={model.onOpenAssetManager}
            leftIcon={<Layers3 className="h-3.5 w-3.5" />}
          >
            火山素材库
          </Button>
          <p className="min-w-0 flex-1 type-caption leading-5 text-[var(--fg-2)]">
            {constraintSummary
              ? `参考图 ${constraintSummary}。点击素材可预览。`
              : "点击素材可预览，点击文字可插入引用。"}
          </p>
        </div>
        <div className="flex min-w-0 gap-2 overflow-x-auto pb-1">
          {model.items.map((item) => (
            <ReferenceChip
              key={item._key}
              item={item}
              active={promptContainsReferenceMention(model.prompt, item)}
              onInsert={() => model.onInsert(item)}
              onPreview={() => model.onPreview(item)}
              onRemove={() => model.onRemove(item)}
            />
          ))}
          {model.items.length === 0 && (
            <button
              type="button"
              disabled={model.pending}
              onClick={() => fileInputRef.current?.click()}
              className="flex min-h-24 min-w-[min(240px,calc(100vw-3rem))] flex-col items-center justify-center gap-2 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-1)]/50 px-5 text-center type-caption text-[var(--fg-2)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] disabled:pointer-events-none disabled:opacity-60"
            >
              <Upload className="h-4 w-4" />
              添加参考
            </button>
          )}
        </div>
      </div>
      <details className="group border-t border-[var(--border-subtle)]">
        <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 type-caption font-medium text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]">
          <span className="inline-flex items-center gap-2">
            <Tags className="h-3.5 w-3.5 text-[var(--fg-2)]" />
            添加官方素材 ID
          </span>
          <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
        </summary>
        <div className="grid grid-cols-1 gap-2 border-t border-[var(--border-subtle)] bg-[var(--bg-1)]/56 p-3 min-[390px]:grid-cols-[auto_minmax(0,1fr)_auto] min-[390px]:items-center">
          <div className="inline-flex h-11 w-full shrink-0 overflow-hidden rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-0.5 min-[390px]:w-auto">
            {model.kindOptions.map((kind) => {
              const active = model.selectedKind === kind;
              return (
                <button
                  key={kind}
                  type="button"
                  aria-pressed={active}
                  disabled={model.pending}
                  onClick={() => model.onKindChange(kind)}
                  className={cn(
                    "inline-flex min-w-12 flex-1 items-center justify-center rounded-[calc(var(--radius-control)-2px)] px-2.5 type-caption font-semibold transition-colors",
                    active
                      ? "bg-[var(--accent)] text-[var(--accent-on)]"
                      : "text-[var(--fg-2)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
                  )}
                >
                  {referenceKindNoun(kind)}
                </button>
              );
            })}
          </div>
          <div className="relative min-w-0 flex-1">
            <Tags className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-2)]" />
            <input
              value={model.assetUrlInput}
              disabled={model.pending}
              onChange={(event) =>
                model.onAssetUrlInputChange(event.target.value)
              }
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.nativeEvent.isComposing &&
                  !model.pending
                ) {
                  event.preventDefault();
                  model.onAddAssetReference();
                }
              }}
              placeholder="asset://asset-..."
              className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] pl-9 pr-3 font-mono type-body text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--accent)]/60 sm:h-10 "
            />
          </div>
          <Button
            variant="secondary"
            size="sm"
            disabled={model.pending || !model.assetUrlInput.trim()}
            onClick={model.onAddAssetReference}
          >
            添加素材
          </Button>
        </div>
      </details>
    </section>
  );
}

function VideoSourceSection({
  action,
  firstFrame,
  references,
}: Pick<VideoPageViewModel["composer"], "action" | "firstFrame" | "references">) {
  if (action === "i2v") return <FirstFrameSection model={firstFrame} />;
  if (action === "reference") return <ReferenceSection model={references} />;
  return null;
}

function VideoPageOverlays({
  model,
}: {
  model: VideoPageViewModel;
}) {
  return (
    <>
      <VolcanoAssetManager
        open={model.assetManager.open}
        model={model.assetManager.model}
        remainingLimits={model.assetManager.remainingLimits}
        existingAssetIds={model.assetManager.existingAssetIds}
        onClose={model.assetManager.onClose}
        onUse={model.assetManager.onUse}
        onDeleted={model.assetManager.onDeleted}
      />
      <VideoTaskDrawer
        open={model.tasks.open}
        onClose={model.tasks.onClose}
        activeItems={model.tasks.activeItems}
        historyItems={model.tasks.historyItems}
        historyFilter={model.tasks.historyFilter}
        historyCounts={model.tasks.historyCounts}
        historyLoading={model.tasks.historyLoading}
        historyError={model.tasks.historyError}
        cancelPendingId={model.tasks.cancelPendingId}
        historyHasNextPage={model.tasks.historyHasNextPage}
        historyFetchingNextPage={model.tasks.historyFetchingNextPage}
        retryDisabled={model.tasks.retryDisabled}
        selectedVideoId={model.tasks.selectedVideoId}
        onHistoryFilterChange={model.tasks.onHistoryFilterChange}
        onRefresh={model.tasks.onRefresh}
        onLoadMore={model.tasks.onLoadMore}
        onCancel={model.tasks.onCancel}
        onRetry={model.tasks.onRetry}
        onCopy={model.tasks.onCopy}
        onUseDraft={model.tasks.onUseDraft}
        onDelete={model.tasks.onDelete}
        onPreview={model.tasks.onPreview}
      />
      {model.playback.item && (
        <VideoPreviewDialog
          item={model.playback.item}
          onClose={model.playback.onClose}
          onUseDraft={model.playback.onUseDraft}
          onRetry={model.playback.onRetry}
          onCopy={model.playback.onCopy}
          onDelete={model.playback.onDelete}
        />
      )}
      {model.referencePreview.item && (
        <ReferenceMediaPreviewDialog
          item={model.referencePreview.item}
          onClose={model.referencePreview.onClose}
          onInsert={model.referencePreview.onInsert}
        />
      )}
    </>
  );
}

export function VideoPageView({ model }: { model: VideoPageViewModel }) {
  const wide = useMediaQuery("(min-width: 1120px)");
  const [parametersOpen, setParametersOpen] = useState(false);
  const openParameters = () => {
    if (wide) model.header.onOpenParameters();
    else setParametersOpen(true);
  };
  const parameterPanel = (
    <VideoParameterPanel
      {...model.parameters}
      className="scroll-mt-20 min-[1120px]:sticky min-[1120px]:top-[76px]"
      onSubmit={() => {
        setParametersOpen(false);
        model.parameters.onSubmit();
      }}
    />
  );
  return (
    <div className="page-shell h-[100dvh] overflow-hidden">
      <div className="hidden md:block">
        <DesktopTopNav active="video" />
      </div>
      <main className="page-scroll page-frame lumen-studio-bg flex flex-col gap-4 [scroll-padding-bottom:calc(var(--mobile-tabbar-height)+6rem)] max-md:pb-[calc(var(--mobile-tabbar-height)+2rem)] md:[scroll-padding-bottom:1rem]">
        <VideoWorkbenchHeader
          mode={actionLabel(model.header.action)}
          profile={model.header.parameterProfile}
          audio={model.header.generateAudio}
          enabled={model.header.serviceEnabled}
          loading={model.header.optionsLoading}
          activeCount={model.header.activeCount}
          historyCount={model.header.historyCount}
          serviceSummary={model.header.serviceSummary}
          submitState={model.header.submitDisabledReason}
          onOpenParameters={openParameters}
          onOpenTasks={model.header.onOpenTasks}
        />
        <div className="grid gap-4 min-[1120px]:grid-cols-[minmax(0,1fr)_340px] min-[1120px]:items-start 2xl:grid-cols-[minmax(0,1fr)_360px]">
          <section className="min-w-0 space-y-4">
            <VideoDirectorViewport
              item={model.viewport.item}
              loading={model.viewport.loading}
              error={model.viewport.error}
              onRetry={model.viewport.onRetry}
              action={model.composer.action}
              prompt={model.composer.prompt.value}
              sourceReady={model.parameters.sourceReady}
              onPreview={model.viewport.onPreview}
            />
            <div className="flex flex-col overflow-hidden border-y border-[var(--border)] bg-transparent">
              <ModeSelector
                action={model.composer.action}
                actionOptions={model.composer.actionOptions}
                onActionChange={model.composer.onActionChange}
              />
              <div className="space-y-3 p-3 sm:p-4 md:pb-5 lg:pb-6">
                <VideoSourceSection
                  action={model.composer.action}
                  firstFrame={model.composer.firstFrame}
                  references={model.composer.references}
                />
                <VideoPromptEditor model={model.composer.prompt} />
                {!wide && (
                  <div className="space-y-2 border-t border-[var(--border-subtle)] pt-3">
                    <div className="flex flex-wrap items-center justify-between gap-2 type-caption text-[var(--fg-2)]">
                      <span>{model.header.parameterProfile}</span>
                      <span>{model.parameters.estimate ? `预计预扣 ${formatMicroRmb(model.parameters.estimate.micro)}` : "费用暂不可估算"}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Button variant="outline" onClick={openParameters} aria-haspopup="dialog" leftIcon={<Settings2 className="h-4 w-4" />}>参数</Button>
                      <Button className="min-w-0 flex-1" disabled={!model.parameters.canSubmit} loading={model.parameters.loading} onClick={model.parameters.onSubmit} leftIcon={<Send className="h-4 w-4" />}>{model.parameters.loading ? "提交中" : "生成视频"}</Button>
                    </div>
                    {model.parameters.reason && <p role="status" className="break-words type-caption text-[var(--fg-2)]">{model.parameters.reason}</p>}
                  </div>
                )}
              </div>
            </div>
          </section>
          {wide ? parameterPanel : null}
        </div>
      </main>
      {!wide && <BottomSheet open={parametersOpen} onClose={() => setParametersOpen(false)} ariaLabel="视频生成参数">{parameterPanel}</BottomSheet>}
      <VideoPageOverlays model={model} />
      <div className="md:hidden">
        <MobileTabBar />
      </div>
    </div>
  );
}
