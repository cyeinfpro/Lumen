"use client";

// Editorial 重构：杂志大标题 + hairline section + portrait thumb + 去三层卡。
// 任务中心：展示用户所有 apparel-model-library job
// - origin=library_generate（独立生成）
// - origin=project_candidate（项目里调 useCreateModelCandidatesMutation 派发的）

import { motion } from "framer-motion";
import {
  AlertTriangle,
  Bookmark,
  CheckCircle2,
  Eraser,
  ExternalLink,
  ImageIcon,
  Library,
  Maximize2,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { toast } from "@/components/ui/primitives/Toast";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";
import { cn } from "@/lib/utils";
import type { LightboxItem } from "@/components/ui/lightbox/types";
import type {
  ApparelModelLibraryJob,
  ApparelModelLibraryJobItem,
  ApparelModelLibraryJobStatus,
  ApparelModelLibrarySaveJobItemIn,
  ModelLibraryAppearance,
  ModelLibraryItemAgeSegment,
} from "@/lib/apiClient";
import {
  MODEL_LIBRARY_APPEARANCE_LABEL,
  MODEL_LIBRARY_APPEARANCE_SELECT_OPTIONS,
} from "@/lib/apiClient";
import {
  useApparelModelLibraryJobsInfiniteQuery,
  useClearApparelModelLibraryJobsMutation,
  useDeleteApparelModelLibraryJobMutation,
  useSaveApparelModelLibraryJobItemMutation,
} from "@/lib/queries";
import { useUiStore } from "@/store/useUiStore";
import { formatRelativeTime } from "../utils";
import {
  AGE_LABEL,
  buildReferenceSummary,
} from "./ModelLibraryJobsModel";

function jobItemToLightboxItem(item: ApparelModelLibraryJobItem): LightboxItem {
  return {
    id: item.image_id,
    url: item.image_url,
    thumbUrl: item.thumb_url ?? undefined,
    previewUrl: item.display_url ?? item.image_url,
    prompt: item.style_tags.join("、") || undefined,
    filename: item.download_filename ?? undefined,
  };
}

function isFreeJobItem(item: ApparelModelLibraryJobItem): boolean {
  return (
    item.billing_free === true ||
    item.billing_label === "free" ||
    item.is_dual_race_bonus === true
  );
}

function openJobLightbox(items: ApparelModelLibraryJobItem[], initialId: string) {
  if (items.length === 0) return;
  const lightboxItems = items.map(jobItemToLightboxItem);
  useUiStore.getState().openLightboxFromItems(lightboxItems, initialId);
}

type AppearanceKey = keyof typeof MODEL_LIBRARY_APPEARANCE_LABEL;

const STATUS_LABEL: Record<ApparelModelLibraryJobStatus, string> = {
  queued: "排队中",
  running: "生成中",
  succeeded: "已完成",
  failed: "失败",
  partial: "部分成功",
};

const ORIGIN_LABEL: Record<"library_generate" | "project_candidate", string> = {
  library_generate: "独立生成",
  project_candidate: "项目候选",
};

export function Stat({
  label,
  value,
  accent = false,
}: {
  label: string;
  value: number;
  accent?: boolean;
}) {
  return (
    <div className="bg-[var(--bg-0)] px-3 py-3 md:px-4">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        {label}
      </p>
      <p
        className={cn(
          "type-metric mt-1 md:text-[22px]",
          accent ? "text-[var(--amber-300)]" : "text-[var(--fg-0)]",
        )}
      >
        {String(value).padStart(2, "0")}
      </p>
    </div>
  );
}

export function Section({
  title,
  eyebrow,
  count,
  children,
}: {
  title: string;
  eyebrow: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="grid gap-3">
      <div className="flex items-baseline gap-3 border-t border-[var(--border)] pt-3">
        <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          {eyebrow}
        </span>
        <h3 className="text-[16px] font-semibold leading-tight text-[var(--fg-0)] md:text-[18px]">
          {title}
        </h3>
        <span className="font-mono text-[11px] tabular-nums text-[var(--fg-2)]">
          {String(count).padStart(2, "0")}
        </span>
      </div>
      {children}
    </section>
  );
}

export function EmptyLine({ label }: { label: string }) {
  return (
    <p className="border-y border-[var(--border)] py-8 text-center font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
      {label}
    </p>
  );
}

export function RunningJobCard({ job }: { job: ApparelModelLibraryJob }) {
  const progress =
    job.requested_count > 0
      ? Math.min(100, Math.round((job.finished_count / job.requested_count) * 100))
      : 0;
  return (
    <motion.article
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      className="grid gap-4 border-t border-[var(--border)] pt-5"
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            <StatusBadge status={job.status} />
            <span aria-hidden className="text-[var(--fg-3)]">·</span>
            <span>{ORIGIN_LABEL[job.origin]}</span>
            {job.origin === "library_generate" ? (
              <>
                <span aria-hidden className="text-[var(--fg-3)]">·</span>
                <SourceBadge job={job} />
              </>
            ) : null}
            {job.project_title ? (
              <>
                <span aria-hidden className="text-[var(--fg-3)]">·</span>
                <Link
                  href={`/projects/${job.workflow_run_id}`}
                  className="inline-flex items-center gap-1 text-[var(--amber-300)] transition-colors hover:text-[var(--amber-200)]"
                >
                  {job.project_title}
                  <ExternalLink className="h-3 w-3" />
                </Link>
              </>
            ) : null}
          </div>
          <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
            <span className="tabular-nums text-[var(--fg-1)]">{job.finished_count}</span>
            <span className="mx-1 text-[var(--fg-3)]">/</span>
            <span className="tabular-nums">{job.requested_count}</span>
            <span aria-hidden className="mx-2 text-[var(--fg-3)]">·</span>
            {formatRelativeTime(job.created_at)}
          </p>
        </div>
        <BriefMeta job={job} />
      </header>
      <ReferenceSummary job={job} compact />
      <ProgressBar value={progress} />
      {job.items.length > 0 ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 md:grid-cols-6">
          {job.items.map((item, idx) => (
            <JobThumb
              key={item.image_id}
              item={item}
              compact
              order={idx}
              onOpenLightbox={() => openJobLightbox(job.items, item.image_id)}
            />
          ))}
        </div>
      ) : null}
      {job.candidates.length > 0 ? (
        <CandidatesGroup job={job} candidates={job.candidates} compact />
      ) : null}
    </motion.article>
  );
}

export function FinishedJobCard({ job }: { job: ApparelModelLibraryJob }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const deleteJob = useDeleteApparelModelLibraryJobMutation({
    onSuccess: () => toast.success("任务已清理"),
    onError: (err) =>
      toast.error("清理失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const dotTone =
    job.status === "succeeded"
      ? "bg-[var(--success)]"
      : job.status === "failed"
        ? "bg-[var(--danger)]"
        : "bg-[var(--amber-300)]";
  return (
    <motion.article
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      className="grid gap-4 border-t border-[var(--border)] pt-5"
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden className={cn("inline-block h-1.5 w-1.5 rounded-full", dotTone)} />
              {STATUS_LABEL[job.status]}
            </span>
            <span aria-hidden className="text-[var(--fg-3)]">·</span>
            <span>{ORIGIN_LABEL[job.origin]}</span>
            {job.origin === "library_generate" ? (
              <>
                <span aria-hidden className="text-[var(--fg-3)]">·</span>
                <SourceBadge job={job} />
              </>
            ) : null}
            {job.project_title ? (
              <>
                <span aria-hidden className="text-[var(--fg-3)]">·</span>
                <Link
                  href={`/projects/${job.workflow_run_id}`}
                  className="inline-flex items-center gap-1 text-[var(--amber-300)] transition-colors hover:text-[var(--amber-200)]"
                >
                  {job.project_title}
                  <ExternalLink className="h-3 w-3" />
                </Link>
              </>
            ) : null}
          </div>
          <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
            <span className="tabular-nums text-[var(--fg-1)]">{job.finished_count}</span>
            <span className="mx-2 text-[var(--fg-3)]">·</span>
            {formatRelativeTime(job.updated_at ?? job.created_at)}
          </p>
          {job.error_message ? (
            <p
              role="alert"
              className="mt-2 max-w-xl text-[12px] leading-[1.6] text-[var(--danger)]"
            >
              {job.error_message}
            </p>
          ) : null}
        </div>
        <div className="flex max-w-full flex-wrap items-start justify-end gap-2">
          <BriefMeta job={job} />
          {job.origin === "library_generate" ? (
            <button
              type="button"
              onClick={() => {
                if (!confirmDelete) {
                  setConfirmDelete(true);
                  window.setTimeout(() => setConfirmDelete(false), 3000);
                  return;
                }
                deleteJob.mutate(job.workflow_run_id);
              }}
              disabled={deleteJob.isPending}
              className={cn(
                "inline-flex min-h-11 items-center gap-1 px-2 font-mono text-[10px] uppercase tracking-[0.16em] transition-colors disabled:cursor-not-allowed disabled:opacity-50 md:h-8 md:min-h-0",
                confirmDelete
                  ? "text-[var(--danger)]"
                  : "text-[var(--fg-2)] hover:text-[var(--danger)]",
              )}
            >
              {deleteJob.isPending ? (
                <Spinner size={12} />
              ) : (
                <Trash2 className="h-3 w-3" />
              )}
              {confirmDelete ? "确认" : "删除"}
            </button>
          ) : null}
        </div>
      </header>
      <ReferenceSummary job={job} />
      {job.requested_count > 0 && job.status !== "succeeded" ? (
        <ProgressBar
          value={Math.min(100, Math.round((job.finished_count / job.requested_count) * 100))}
        />
      ) : null}
      {job.items.length === 0 ? (
        <EmptyLine label="没有已落地的图像" />
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5">
          {job.items.map((item, idx) => (
            <JobThumb
              key={item.image_id}
              item={item}
              job={job}
              order={idx}
              onOpenLightbox={() => openJobLightbox(job.items, item.image_id)}
            />
          ))}
        </div>
      )}
      {job.candidates.length > 0 ? (
        <CandidatesGroup job={job} candidates={job.candidates} />
      ) : null}
    </motion.article>
  );
}

// 候选区：双路竞速另一路供应商的产出，不参与 finished_count，但可按需收藏入库。
function CandidatesGroup({
  job,
  candidates,
  compact = false,
}: {
  job: ApparelModelLibraryJob;
  candidates: ApparelModelLibraryJobItem[];
  compact?: boolean;
}) {
  const lightboxItems = useMemo(
    () => candidates.map(jobItemToLightboxItem),
    [candidates],
  );
  const open = (initialId: string) => {
    if (lightboxItems.length === 0) return;
    useUiStore.getState().openLightboxFromItems(lightboxItems, initialId);
  };
  return (
    <section className="grid gap-3 border-t border-[var(--border)] pt-4">
      <header className="grid gap-1">
        <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          候选 · 竞速产出
        </p>
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-3)]">
          另一路供应商的产出，可预览或入库
        </p>
      </header>
      <div
        className={cn(
          "grid gap-3",
          compact
            ? "grid-cols-2 sm:grid-cols-4 md:grid-cols-6"
            : "grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5",
        )}
      >
        {candidates.map((item, idx) => (
          <JobThumb
            key={item.image_id}
            item={item}
            job={job}
            compact={compact}
            order={idx}
            onOpenLightbox={() => open(item.image_id)}
          />
        ))}
      </div>
    </section>
  );
}

function SourceBadge({ job }: { job: ApparelModelLibraryJob }) {
  const reference = Boolean(job.reference_image_id);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1",
        reference ? "text-[var(--amber-300)]" : "text-[var(--fg-2)]",
      )}
    >
      {reference ? <ImageIcon className="h-3 w-3" /> : null}
      {reference ? "参考图" : "文生"}
    </span>
  );
}

function ReferenceSummary({
  job,
  compact = false,
}: {
  job: ApparelModelLibraryJob;
  compact?: boolean;
}) {
  const summary = buildReferenceSummary(job);
  if (!summary) return null;
  return (
    <section
      className={cn(
        "grid gap-3 border border-[var(--border)] bg-[var(--bg-1)] p-3",
        compact ? "sm:grid-cols-[72px_minmax(0,1fr)]" : "sm:grid-cols-[88px_minmax(0,1fr)]",
      )}
    >
      <div
        className={cn(
          "relative overflow-hidden bg-[var(--bg-2)]",
          compact ? "aspect-[4/5] w-[72px]" : "aspect-[4/5] w-[88px]",
        )}
      >
        <Image
          src={summary.imageUrl}
          alt="参考图"
          fill
          unoptimized
          sizes="88px"
          className="object-cover"
        />
      </div>
      <div className="min-w-0 self-center">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          参考图识别
        </p>
        {summary.tokens.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {summary.tokens.map((token, idx) => (
              <span
                key={`${token}-${idx}`}
                className="max-w-full break-words border border-[var(--border)] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--fg-1)]"
              >
                {token}
              </span>
            ))}
          </div>
        ) : (
          <p className="mt-1 text-[12px] text-[var(--fg-3)]">
            未返回可展示的识别字段
          </p>
        )}
        {summary.notes ? (
          <p className="mt-2 line-clamp-2 text-[12px] leading-[1.55] text-[var(--fg-2)]">
            {summary.notes}
          </p>
        ) : null}
      </div>
    </section>
  );
}

function BriefMeta({ job }: { job: ApparelModelLibraryJob }) {
  const tokens: string[] = [];
  if (job.age_segment) tokens.push(AGE_LABEL[job.age_segment]);
  if (job.gender) tokens.push(job.gender === "male" ? "男" : "女");
  if (job.appearance_direction) {
    const key = job.appearance_direction as AppearanceKey;
    tokens.push(MODEL_LIBRARY_APPEARANCE_LABEL[key] ?? job.appearance_direction);
  }
  if (tokens.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
      {tokens.map((token, idx) => (
        <span key={`${token}-${idx}`} className="inline-flex items-center gap-2">
          {idx > 0 ? <span aria-hidden className="text-[var(--fg-3)]">·</span> : null}
          <span>{token}</span>
        </span>
      ))}
    </div>
  );
}

// 状态徽标：dot + mono caption；running 自带 spinner
export function StatusBadge({ status }: { status: ApparelModelLibraryJobStatus }) {
  const dot =
    status === "queued"
      ? "bg-[var(--fg-3)]"
      : status === "running"
        ? "bg-[var(--amber-400)] animate-[lumen-pulse-soft_1800ms_ease-in-out_infinite]"
        : status === "succeeded"
          ? "bg-[var(--success)]"
          : status === "failed"
            ? "bg-[var(--danger)]"
            : "bg-[var(--amber-300)]";
  const tone =
    status === "running" || status === "succeeded" || status === "failed" || status === "partial"
      ? "text-[var(--fg-1)]"
      : "text-[var(--fg-2)]";
  return (
    <span className={cn("inline-flex items-center gap-1.5", tone)}>
      {status === "running" ? (
        <Spinner size={12} />
      ) : status === "succeeded" ? (
        <CheckCircle2 className="h-3 w-3 text-[var(--success)]" />
      ) : status === "failed" || status === "partial" ? (
        <AlertTriangle className="h-3 w-3 text-[var(--danger)]" />
      ) : (
        <span aria-hidden className={cn("inline-block h-1.5 w-1.5 rounded-full", dot)} />
      )}
      {STATUS_LABEL[status]}
    </span>
  );
}

// amber 进度条
function ProgressBar({ value }: { value: number }) {
  return (
    <div className="grid gap-1.5">
      <div className="h-px overflow-hidden bg-[var(--border)]">
        <div
          className="h-full bg-[var(--amber-400)] transition-[width] duration-300"
          style={{ width: `${value}%` }}
        />
      </div>
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] tabular-nums text-[var(--fg-2)]">
        {String(value).padStart(2, "0")}%
      </p>
    </div>
  );
}

function JobThumb({
  item,
  job,
  compact = false,
  disableSaveAction = false,
  onOpenLightbox,
  order,
}: {
  item: ApparelModelLibraryJobItem;
  job?: ApparelModelLibraryJob;
  compact?: boolean;
  disableSaveAction?: boolean;
  onOpenLightbox?: () => void;
  order?: number;
}) {
  const [saveOpen, setSaveOpen] = useState(false);
  const model = buildJobThumbModel(item, job, disableSaveAction);

  return (
    <div className="group relative">
      <JobThumbnailMedia
        compact={compact}
        free={model.free}
        item={item}
        order={order}
        saved={model.saved}
        onOpenLightbox={onOpenLightbox}
      />
      <JobThumbCaption
        compact={compact}
        item={item}
        model={model}
        onSave={() => setSaveOpen(true)}
      />
      <CompactJobThumbSaveAction
        compact={compact}
        model={model}
        onSave={() => setSaveOpen(true)}
      />
      <JobThumbSaveDialog
        allowSave={model.allowSave}
        item={item}
        job={job}
        open={saveOpen}
        onClose={() => setSaveOpen(false)}
      />
    </div>
  );
}

interface JobThumbModel {
  allowSave: boolean;
  appearanceLabel: string;
  canSave: boolean;
  free: boolean;
  saved: boolean;
}

function buildJobThumbModel(
  item: ApparelModelLibraryJobItem,
  job: ApparelModelLibraryJob | undefined,
  disableSaveAction: boolean,
): JobThumbModel {
  const appearanceKey = (
    item.appearance_direction ||
    job?.appearance_direction ||
    ""
  ) as AppearanceKey | "";
  return {
    allowSave: !disableSaveAction,
    appearanceLabel: appearanceKey
      ? (MODEL_LIBRARY_APPEARANCE_LABEL[appearanceKey] ?? appearanceKey)
      : "",
    canSave: Boolean(job) && !disableSaveAction,
    free: isFreeJobItem(item),
    saved: item.saved_item_id != null,
  };
}

function JobThumbCaption({
  compact,
  item,
  model,
  onSave,
}: {
  compact: boolean;
  item: ApparelModelLibraryJobItem;
  model: JobThumbModel;
  onSave: () => void;
}) {
  if (compact) return null;
  const caption =
    [model.appearanceLabel, item.style_tags.slice(0, 2).join("、")]
      .filter(Boolean)
      .join(" · ") || "未识别";
  return (
    <div className="mt-2.5 flex min-w-0 items-center justify-between gap-2">
      <span className="min-w-0 flex-1 truncate font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--fg-2)] min-[390px]:tracking-[0.16em]">
        {caption}
      </span>
      {model.canSave && !model.saved ? (
        <button
          type="button"
          aria-label="收藏入库"
          onClick={onSave}
          className="inline-flex min-h-11 shrink-0 items-center gap-1 px-2 font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--amber-300)] transition-colors hover:text-[var(--amber-200)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 min-[390px]:tracking-[0.16em] md:h-7 md:min-h-0"
        >
          <Bookmark className="h-3 w-3" />
          入库
        </button>
      ) : null}
    </div>
  );
}

function CompactJobThumbSaveAction({
  compact,
  model,
  onSave,
}: {
  compact: boolean;
  model: JobThumbModel;
  onSave: () => void;
}) {
  if (!compact || !model.canSave || model.saved) return null;
  return (
    <button
      type="button"
      aria-label="收藏入库"
      onClick={onSave}
      className="absolute right-2 top-2 inline-flex h-11 w-11 items-center justify-center rounded-full bg-[var(--accent)] text-[var(--bg-0)] shadow-[var(--shadow-1)] transition-opacity hover:bg-[var(--amber-200)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:h-7 md:w-7 md:opacity-0 md:group-hover:opacity-100"
    >
      <Bookmark className="h-3.5 w-3.5" />
    </button>
  );
}

function JobThumbSaveDialog({
  allowSave,
  item,
  job,
  open,
  onClose,
}: {
  allowSave: boolean;
  item: ApparelModelLibraryJobItem;
  job?: ApparelModelLibraryJob;
  open: boolean;
  onClose: () => void;
}) {
  if (!open || !job || !allowSave) return null;
  return <SaveJobItemDialog item={item} job={job} onClose={onClose} />;
}

function JobThumbnailMedia({
  compact,
  free,
  item,
  order,
  saved,
  onOpenLightbox,
}: {
  compact: boolean;
  free: boolean;
  item: ApparelModelLibraryJobItem;
  order?: number;
  saved: boolean;
  onOpenLightbox?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpenLightbox}
      aria-label="查看大图"
      className={cn(
        "relative block w-full cursor-zoom-in overflow-hidden bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
        compact ? "aspect-square" : "aspect-[3/4]",
      )}
    >
      <Image
        src={item.thumb_url || item.image_url}
        alt="生成模特"
        fill
        unoptimized
        sizes="(max-width: 768px) 50vw, 220px"
        className="object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
      />
      {typeof order === "number" ? (
        <span className="absolute left-2 top-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/85 mix-blend-difference">
          N°{String(order + 1).padStart(2, "0")}
        </span>
      ) : null}
      {free ? (
        <span className="absolute right-2 top-2 inline-flex rounded-full border border-white/20 bg-black/60 px-2 py-0.5 font-mono text-[10px] tracking-[0.14em] text-white backdrop-blur">
          free
        </span>
      ) : null}
      {saved ? (
        <span
          className={cn(
            "absolute right-2 inline-flex items-center gap-1 bg-[var(--success)]/90 px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em] text-white backdrop-blur",
            free ? "top-8" : "top-2",
          )}
        >
          <Bookmark className="h-3 w-3" />
          已入库
        </span>
      ) : null}
      {/* @ui-governance-allow media: zoom affordance overlays the generated image. */}
      <span className="pointer-events-none absolute bottom-2 right-2 inline-flex h-7 w-7 items-center justify-center rounded-full bg-black/60 text-white opacity-100 backdrop-blur transition-opacity duration-150 md:opacity-0 md:group-hover:opacity-100">
        <Maximize2 className="h-3.5 w-3.5" />
      </span>
    </button>
  );
}

import { SaveJobItemDialog } from "./ModelLibraryJobDialogs";

export function EmptyJobs() {
  return (
    <section className="border-y border-[var(--border)] py-14 md:py-16">
      <div className="grid gap-6 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--amber-300)]">
            <Library className="mr-1.5 -mt-px inline-block h-3 w-3" />
            空队列
          </p>
          <h4 className="type-page-title mt-3 md:text-[28px]">
            还没有任务
          </h4>
          <p className="type-body mt-3 max-w-xl">
            {`从"新建模特"提交一批，或者在项目里生成模特候选，都会在这里实时聚合。`}
          </p>
        </div>
      </div>
    </section>
  );
}
