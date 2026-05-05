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
  ExternalLink,
  Library,
  Maximize2,
  RefreshCw,
  X,
} from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { toast } from "@/components/ui/primitives/Toast";
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
import { MODEL_LIBRARY_APPEARANCE_LABEL } from "@/lib/apiClient";
import {
  useApparelModelLibraryJobsQuery,
  useSaveApparelModelLibraryJobItemMutation,
} from "@/lib/queries";
import { useUiStore } from "@/store/useUiStore";
import { formatRelativeTime } from "../utils";

function jobItemToLightboxItem(item: ApparelModelLibraryJobItem): LightboxItem {
  return {
    id: item.image_id,
    url: item.image_url,
    thumbUrl: item.thumb_url ?? undefined,
    previewUrl: item.display_url ?? item.image_url,
    prompt: item.style_tags.join("、") || undefined,
  };
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

const AGE_LABEL: Record<ModelLibraryItemAgeSegment, string> = {
  user_favorites: "用户收藏",
  toddler: "幼儿",
  child: "儿童",
  teen: "青少年",
  young_adult: "青年",
  adult: "成年",
  middle_aged: "中老年",
  senior: "老年",
};

export function ModelLibraryJobsPanel() {
  const jobs = useApparelModelLibraryJobsQuery();
  const items = useMemo(() => jobs.data?.items ?? [], [jobs.data?.items]);

  const { running, finished } = useMemo(() => {
    const r: ApparelModelLibraryJob[] = [];
    const f: ApparelModelLibraryJob[] = [];
    for (const job of items) {
      if (job.status === "queued" || job.status === "running") r.push(job);
      else f.push(job);
    }
    return { running: r, finished: f };
  }, [items]);

  return (
    <div className="grid gap-7">
      <header className="border-b border-[var(--border)] pb-6">
        <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0 flex-1">
            <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Jobs Center
            </p>
            <h2 className="mt-2 font-display text-[32px] italic leading-[1] text-[var(--fg-0)] md:text-[40px]">
              任务中心
            </h2>
            <p className="mt-3 max-w-xl text-[13px] leading-6 text-[var(--fg-2)]">
              独立生成与项目候选的统一进度跟踪
            </p>
          </div>
          <button
            type="button"
            aria-label="手动刷新"
            onClick={() => jobs.refetch()}
            disabled={jobs.isFetching}
            className={cn(
              "inline-flex h-10 items-center gap-2 self-start rounded-full border border-[var(--border)] px-3 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] disabled:cursor-not-allowed disabled:opacity-60 md:self-end",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
            )}
          >
            <RefreshCw className={cn("h-3.5 w-3.5", jobs.isFetching && "animate-spin")} />
            <span>Refresh</span>
          </button>
        </div>
        {!jobs.isPending && items.length > 0 ? (
          <div className="mt-6 grid grid-cols-1 gap-px overflow-hidden border border-[var(--border)] sm:grid-cols-3 md:max-w-2xl">
            <Stat label="Total" value={items.length} />
            <Stat label="Active" value={running.length} accent={running.length > 0} />
            <Stat label="Done" value={finished.length} />
          </div>
        ) : null}
      </header>

      {jobs.isPending ? (
        <div className="flex h-40 items-center justify-center gap-2 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          <Spinner size={20} />
          Loading
        </div>
      ) : items.length === 0 ? (
        <EmptyJobs />
      ) : (
        <>
          <Section title="进行中" eyebrow="Active" count={running.length}>
            {running.length === 0 ? (
              <EmptyLine label="目前没有进行中的任务" />
            ) : (
              <div className="grid gap-8">
                {running.map((job) => (
                  <RunningJobCard key={job.job_id} job={job} />
                ))}
              </div>
            )}
          </Section>

          <Section title="已完成 / 失败" eyebrow="Archive" count={finished.length}>
            {finished.length === 0 ? (
              <EmptyLine label="还没有已完成的任务" />
            ) : (
              <div className="grid gap-8">
                {finished.map((job) => (
                  <FinishedJobCard key={job.job_id} job={job} />
                ))}
              </div>
            )}
          </Section>
        </>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  accent = false,
}: {
  label: string;
  value: number;
  accent?: boolean;
}) {
  return (
    <div className="bg-[var(--bg-0)] px-4 py-4 md:px-5 md:py-5">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        {label}
      </p>
      <p
        className={cn(
          "mt-1 text-[24px] font-semibold leading-none tabular-nums md:text-[28px]",
          accent ? "text-[var(--amber-300)]" : "text-[var(--fg-0)]",
        )}
      >
        {String(value).padStart(2, "0")}
      </p>
    </div>
  );
}

function Section({
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
    <section className="grid gap-5">
      <div className="flex items-baseline gap-3 border-t border-[var(--border)] pt-5">
        <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          {eyebrow}
        </span>
        <h3 className="text-[18px] font-semibold leading-tight text-[var(--fg-0)] md:text-[20px]">
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

function EmptyLine({ label }: { label: string }) {
  return (
    <p className="border-y border-[var(--border)] py-8 text-center font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
      {label}
    </p>
  );
}

function RunningJobCard({ job }: { job: ApparelModelLibraryJob }) {
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

function FinishedJobCard({ job }: { job: ApparelModelLibraryJob }) {
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
            <p className="mt-2 max-w-xl text-[12px] leading-[1.6] text-[var(--danger)]">
              {job.error_message}
            </p>
          ) : null}
        </div>
        <BriefMeta job={job} />
      </header>
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

// 候选区：dual_race 另一路 provider 的产出，不参与 finished_count，但可按需收藏入库。
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
          Candidates · 竞速产出
        </p>
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-3)]">
          另一路 provider 的产出，可预览或入库
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

function BriefMeta({ job }: { job: ApparelModelLibraryJob }) {
  const tokens: string[] = [];
  if (job.age_segment) tokens.push(AGE_LABEL[job.age_segment]);
  if (job.gender) tokens.push(job.gender);
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
function StatusBadge({ status }: { status: ApparelModelLibraryJobStatus }) {
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
  const saved = item.saved_item_id != null;
  const allowSave = !disableSaveAction;
  const canSave = Boolean(job && allowSave);
  const appearanceKey = (item.appearance_direction || job?.appearance_direction || "") as
    | AppearanceKey
    | "";
  const appearanceLabel = appearanceKey
    ? (MODEL_LIBRARY_APPEARANCE_LABEL[appearanceKey as AppearanceKey] ?? appearanceKey)
    : "";

  return (
    <div className="group relative">
      <button
        type="button"
        onClick={() => onOpenLightbox?.()}
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
          className="object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.04]"
        />
        {/* N°NN 序号 */}
        {typeof order === "number" ? (
          <span className="absolute left-2 top-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/85 mix-blend-difference">
            N°{String(order + 1).padStart(2, "0")}
          </span>
        ) : null}
        {saved ? (
          <span className="absolute right-2 top-2 inline-flex items-center gap-1 bg-[var(--success)]/90 px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em] text-white backdrop-blur">
            <Bookmark className="h-3 w-3" />
            Saved
          </span>
        ) : null}
        <span className="pointer-events-none absolute bottom-2 right-2 inline-flex h-7 w-7 items-center justify-center rounded-full bg-black/60 text-white opacity-100 backdrop-blur transition-opacity duration-150 md:opacity-0 md:group-hover:opacity-100">
          <Maximize2 className="h-3.5 w-3.5" />
        </span>
      </button>
      {!compact ? (
        <div className="mt-2.5 flex items-center justify-between gap-2">
          <span className="truncate font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
            {[appearanceLabel, item.style_tags.slice(0, 2).join("、")]
              .filter(Boolean)
              .join(" · ") || "未识别"}
          </span>
          {canSave && !saved ? (
            <button
              type="button"
              aria-label="收藏入库"
              onClick={() => setSaveOpen(true)}
              className="inline-flex h-7 items-center gap-1 px-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--amber-300)] transition-colors hover:text-[var(--amber-200)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
            >
              <Bookmark className="h-3 w-3" />
              Save
            </button>
          ) : null}
        </div>
      ) : null}
      {compact && canSave && !saved ? (
        <button
          type="button"
          aria-label="收藏入库"
          onClick={() => setSaveOpen(true)}
          className="absolute right-2 top-2 inline-flex h-7 w-7 items-center justify-center rounded-full bg-[var(--accent)] text-[var(--bg-0)] shadow-[var(--shadow-1)] transition-opacity hover:bg-[var(--amber-200)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:opacity-0 md:group-hover:opacity-100"
        >
          <Bookmark className="h-3.5 w-3.5" />
        </button>
      ) : null}
      {saveOpen && job && allowSave ? (
        <SaveJobItemDialog
          item={item}
          job={job}
          onClose={() => setSaveOpen(false)}
        />
      ) : null}
    </div>
  );
}

function SaveJobItemDialog({
  item,
  job,
  onClose,
}: {
  item: ApparelModelLibraryJobItem;
  job: ApparelModelLibraryJob;
  onClose: () => void;
}) {
  const defaultAge: ModelLibraryItemAgeSegment = job.age_segment ?? "young_adult";
  const defaultGender = job.gender || "female";
  const [title, setTitle] = useState(
    () =>
      `${ORIGIN_LABEL[job.origin]} · ${AGE_LABEL[defaultAge] ?? defaultAge}`,
  );
  const [age, setAge] = useState<ModelLibraryItemAgeSegment>(defaultAge);
  const [gender, setGender] = useState(defaultGender);
  const [appearance, setAppearance] = useState<ModelLibraryAppearance | "">(
    () =>
      (item.appearance_direction || job.appearance_direction || "") as ModelLibraryAppearance | "",
  );
  const [styleTags, setStyleTags] = useState(item.style_tags.join("、"));
  const [autoTag, setAutoTag] = useState(true);

  const save = useSaveApparelModelLibraryJobItemMutation(
    job.workflow_run_id,
    item.image_id,
    {
      onSuccess: () => {
        toast.success("已收藏入库");
        onClose();
      },
      onError: (err) =>
        toast.error("入库失败", {
          description: err instanceof Error ? err.message : "请稍后重试",
        }),
    },
  );

  // ESC 关闭 + body lock
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [onClose]);

  const submit = () => {
    const next = title.trim();
    if (!next) {
      toast.warning("名称不能为空");
      return;
    }
    const body: ApparelModelLibrarySaveJobItemIn = {
      title: next,
      age_segment: age,
      gender,
      appearance_direction: appearance || null,
      style_tags: styleTags
        .split(/[,，、]/)
        .map((tok) => tok.trim())
        .filter(Boolean)
        .slice(0, 12),
      auto_tag: autoTag,
    };
    save.mutate(body);
  };

  return (
    <div
      className="fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/60 backdrop-blur-md mobile-dialog-shell md:items-center md:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label="收藏入库"
        initial={{ opacity: 0, y: 24, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 12, scale: 0.98 }}
        transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
        className="mobile-dialog-panel flex w-full flex-col overflow-hidden border border-[var(--border)] bg-[var(--bg-0)] md:max-h-[92dvh] md:max-w-md"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] px-5 pb-4 pt-5">
          <div>
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Save to library
            </p>
            <h3 className="mt-2 text-[20px] font-semibold leading-tight text-[var(--fg-0)]">
              收藏入库
            </h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-9 w-9 cursor-pointer items-center justify-center text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)]"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="mobile-dialog-scroll grid min-h-0 flex-1 gap-5 overflow-y-auto px-5 py-5">
          <UnderlineLabeled label="名称">
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="高级简洁青年女模特"
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            />
          </UnderlineLabeled>
          <div className="grid gap-5 md:grid-cols-2">
            <UnderlineLabeled label="年龄段">
              <select
                value={age}
                onChange={(event) => setAge(event.target.value as ModelLibraryItemAgeSegment)}
                className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--amber-400)] md:h-10 md:text-sm"
              >
                {(Object.keys(AGE_LABEL) as ModelLibraryItemAgeSegment[]).map((segment) => (
                  <option key={segment} value={segment} className="bg-[var(--bg-0)]">
                    {AGE_LABEL[segment]}
                  </option>
                ))}
              </select>
            </UnderlineLabeled>
            <UnderlineLabeled label="性别">
              <select
                value={gender}
                onChange={(event) => setGender(event.target.value)}
                className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--amber-400)] md:h-10 md:text-sm"
              >
                <option value="female" className="bg-[var(--bg-0)]">女</option>
                <option value="male" className="bg-[var(--bg-0)]">男</option>
              </select>
            </UnderlineLabeled>
          </div>
          <UnderlineLabeled label="外貌方向">
            <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1">
              <Chip active={appearance === ""} onClick={() => setAppearance("")}>
                不指定
              </Chip>
              {(
                Object.entries(MODEL_LIBRARY_APPEARANCE_LABEL) as [
                  Exclude<ModelLibraryAppearance, "all">,
                  string,
                ][]
              ).map(([value, label]) => (
                <Chip
                  key={value}
                  active={appearance === value}
                  onClick={() => setAppearance(value)}
                >
                  {label}
                </Chip>
              ))}
            </div>
          </UnderlineLabeled>
          <UnderlineLabeled label="风格标签">
            <input
              value={styleTags}
              onChange={(event) => setStyleTags(event.target.value)}
              placeholder="高级简洁、棚拍"
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            />
          </UnderlineLabeled>
          <label className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-1)]">
            <input
              type="checkbox"
              checked={autoTag}
              onChange={(event) => setAutoTag(event.target.checked)}
              className="accent-[var(--amber-400)]"
            />
            入库后再跑一次自动识别
          </label>
        </div>
        <footer className="mobile-dialog-footer flex shrink-0 justify-end gap-2 border-t border-[var(--border)] px-5 py-4">
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" loading={save.isPending} onClick={submit}>
            保存
          </Button>
        </footer>
      </motion.div>
    </div>
  );
}

function UnderlineLabeled({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="grid gap-2">
      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        {label}
      </span>
      {children}
    </label>
  );
}

// underline-on-active chip
function Chip({
  children,
  active,
  onClick,
}: {
  children: React.ReactNode;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "group relative inline-flex min-h-10 cursor-pointer items-center px-1 py-1.5 font-mono text-[11px] uppercase tracking-[0.16em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:min-h-9",
        active ? "text-[var(--fg-0)]" : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
      )}
    >
      <span>{children}</span>
      <span
        aria-hidden
        className={cn(
          "absolute inset-x-1 -bottom-px h-px transition-colors duration-[var(--dur-base)]",
          active
            ? "bg-[var(--amber-400)]"
            : "bg-transparent group-hover:bg-[var(--border-strong)]",
        )}
      />
    </button>
  );
}

function EmptyJobs() {
  return (
    <section className="border-y border-[var(--border)] py-14 md:py-16">
      <div className="grid gap-6 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--amber-300)]">
            <Library className="mr-1.5 -mt-px inline-block h-3 w-3" />
            Empty queue
          </p>
          <h4 className="mt-3 text-[24px] font-semibold leading-tight text-[var(--fg-0)] md:text-[28px]">
            还没有任务
          </h4>
          <p className="mt-3 max-w-xl text-[14px] leading-[1.7] text-[var(--fg-1)]">
            {`从"新建模特"提交一批，或者在项目里生成模特候选，都会在这里实时聚合。`}
          </p>
        </div>
      </div>
    </section>
  );
}
