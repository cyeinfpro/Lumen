"use client";

// 任务中心：展示用户所有 apparel-model-library job
// - origin=library_generate（独立生成）
// - origin=project_candidate（项目里调 useCreateModelCandidatesMutation 派发的）
// 上半部分：进行中（queued / running）
// 下半部分：已完成 / 失败 / 部分成功（succeeded / failed / partial）
//
// 已完成 job 的图：点"收藏入库"会弹一个简表，title / age_segment / gender 等都从 job 字段继承。
// 缩略图点击 = 打开统一 Lightbox（与项目页一致的体验，免去 JobImageOverlay）。

import { motion } from "framer-motion";
import {
  AlertTriangle,
  Bookmark,
  CheckCircle2,
  ExternalLink,
  Library,
  Maximize2,
  RefreshCw,
} from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Input } from "@/components/ui/primitives/Input";
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
    previewUrl: item.thumb_url ?? undefined,
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
    <div className="grid gap-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="flex items-center gap-2 font-mono text-[11px] font-medium tracking-[0.16em] text-[var(--fg-2)]">
            <Library className="h-3.5 w-3.5" />
            JOBS CENTER
          </p>
          <h3 className="mt-2 font-display text-[26px] italic leading-tight text-[var(--fg-0)] md:text-[28px]">
            任务中心
          </h3>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
            独立生成与项目候选的统一进度跟踪
          </p>
        </div>
        {/* 移动端 icon-only 圆按钮，桌面端带文字 */}
        <button
          type="button"
          aria-label="手动刷新"
          onClick={() => jobs.refetch()}
          disabled={jobs.isFetching}
          className={cn(
            "inline-flex h-9 w-9 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-1)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)] disabled:cursor-not-allowed disabled:opacity-60",
            "md:h-9 md:w-auto md:rounded-md md:px-3 md:text-xs md:font-medium md:gap-1.5",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          )}
        >
          <RefreshCw className={cn("h-3.5 w-3.5", jobs.isFetching && "animate-spin")} />
          <span className="hidden md:inline">手动刷新</span>
        </button>
      </header>

      {jobs.isPending ? (
        <div className="flex h-40 items-center justify-center gap-2 text-sm text-[var(--fg-2)]">
          <Spinner size={20} />
          加载任务列表
        </div>
      ) : items.length === 0 ? (
        <EmptyJobs />
      ) : (
        <>
          <Section title="进行中" count={running.length}>
            {running.length === 0 ? (
              <p className="rounded-md border border-dashed border-[var(--border)] bg-white/[0.02] px-4 py-6 text-center text-xs text-[var(--fg-2)]">
                目前没有进行中的任务
              </p>
            ) : (
              <div className="grid gap-3">
                {running.map((job) => (
                  <RunningJobCard key={job.job_id} job={job} />
                ))}
              </div>
            )}
          </Section>

          <Section title="已完成 / 失败" count={finished.length}>
            {finished.length === 0 ? (
              <p className="rounded-md border border-dashed border-[var(--border)] bg-white/[0.02] px-4 py-6 text-center text-xs text-[var(--fg-2)]">
                还没有已完成的任务
              </p>
            ) : (
              <div className="grid gap-3">
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

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="grid gap-2">
      <h4 className="flex items-center gap-2 text-sm font-medium text-[var(--fg-1)]">
        {title}
        <span className="rounded-full border border-[var(--border)] bg-white/[0.04] px-2 py-0.5 text-[10px] text-[var(--fg-2)]">
          {count}
        </span>
      </h4>
      {children}
    </section>
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
      className="grid gap-3 rounded-xl border border-[var(--border-amber)]/60 bg-[var(--bg-1)] p-3 shadow-[var(--shadow-1)]"
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={job.status} />
            <Badge>{ORIGIN_LABEL[job.origin]}</Badge>
            {job.project_title ? (
              <Link
                href={`/projects/${job.workflow_run_id}`}
                className="inline-flex items-center gap-1 text-xs text-[var(--amber-300)] hover:underline"
              >
                {job.project_title}
                <ExternalLink className="h-3 w-3" />
              </Link>
            ) : null}
          </div>
          <p className="mt-1 text-xs text-[var(--fg-2)]">
            已完成 {job.finished_count} / {job.requested_count} 张 · 创建于{" "}
            {formatRelativeTime(job.created_at)}
          </p>
        </div>
        <BriefMeta job={job} />
      </header>
      <ProgressBar value={progress} />
      {job.items.length > 0 ? (
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6">
          {job.items.map((item) => (
            <JobThumb
              key={item.image_id}
              item={item}
              compact
              onOpenLightbox={() => openJobLightbox(job.items, item.image_id)}
            />
          ))}
        </div>
      ) : null}
      {job.candidates.length > 0 ? (
        <CandidatesGroup candidates={job.candidates} compact />
      ) : null}
    </motion.article>
  );
}

function FinishedJobCard({ job }: { job: ApparelModelLibraryJob }) {
  const tone =
    job.status === "succeeded"
      ? "border-[var(--success)]/30"
      : job.status === "failed"
        ? "border-[var(--danger)]/40"
        : "border-[var(--border-amber)]/40";
  return (
    <motion.article
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      className={cn(
        "grid gap-3 rounded-xl border bg-[var(--bg-1)] p-3 shadow-[var(--shadow-1)]",
        tone,
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={job.status} />
            <Badge>{ORIGIN_LABEL[job.origin]}</Badge>
            {job.project_title ? (
              <Link
                href={`/projects/${job.workflow_run_id}`}
                className="inline-flex items-center gap-1 text-xs text-[var(--amber-300)] hover:underline"
              >
                {job.project_title}
                <ExternalLink className="h-3 w-3" />
              </Link>
            ) : null}
          </div>
          <p className="mt-1 text-xs text-[var(--fg-2)]">
            产出 {job.finished_count} 张 · {formatRelativeTime(job.updated_at ?? job.created_at)}
          </p>
          {job.error_message ? (
            <p className="mt-1 text-xs text-[var(--danger)]">{job.error_message}</p>
          ) : null}
        </div>
        <BriefMeta job={job} />
      </header>
      {/* 进度条：partial 时仍能看出"差几张" */}
      {job.requested_count > 0 && job.status !== "succeeded" ? (
        <ProgressBar
          value={Math.min(100, Math.round((job.finished_count / job.requested_count) * 100))}
        />
      ) : null}
      {job.items.length === 0 ? (
        <p className="rounded-md border border-dashed border-[var(--border)] bg-white/[0.02] px-3 py-4 text-center text-xs text-[var(--fg-2)]">
          没有已落地的图像
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5">
          {job.items.map((item) => (
            <JobThumb
              key={item.image_id}
              item={item}
              job={job}
              onOpenLightbox={() => openJobLightbox(job.items, item.image_id)}
            />
          ))}
        </div>
      )}
      {job.candidates.length > 0 ? (
        <CandidatesGroup candidates={job.candidates} />
      ) : null}
    </motion.article>
  );
}

// 候选区：dual_race 另一路 provider 的产出。语义最弱（不可入库、不参与 finished_count），
// 视觉上跟 unsaved/saved 拉开差距：dashed border + 一行小字说明 + 用 mini label 标头。
function CandidatesGroup({
  candidates,
  compact = false,
}: {
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
    <section className="grid gap-2 rounded-lg border border-dashed border-[var(--border)] bg-white/[0.02] p-2.5">
      <header className="grid gap-0.5">
        <p className="font-mono text-[11px] tracking-[0.16em] text-[var(--fg-2)]">
          CANDIDATES · 竞速产出
        </p>
        <p className="text-[11px] text-[var(--fg-2)]">
          另一路 provider 的产出，可点击预览
        </p>
      </header>
      <div
        className={cn(
          "grid gap-2",
          compact
            ? "grid-cols-3 sm:grid-cols-4 md:grid-cols-6"
            : "grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5",
        )}
      >
        {candidates.map((item) => (
          <JobThumb
            key={item.image_id}
            item={item}
            compact={compact}
            disableSaveAction
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
    <div className="flex flex-wrap gap-1.5 text-[11px] text-[var(--fg-2)]">
      {tokens.map((token, idx) => (
        <span
          key={`${token}-${idx}`}
          className="rounded-full border border-[var(--border)] bg-white/[0.04] px-2 py-0.5"
        >
          {token}
        </span>
      ))}
    </div>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-full border border-[var(--border)] bg-white/[0.04] px-2 py-0.5 text-[10px] text-[var(--fg-2)]">
      {children}
    </span>
  );
}

// 状态胶囊：5 种状态 5 种色调；running 自带 spinner，succeeded/failed 自带 icon
function StatusBadge({ status }: { status: ApparelModelLibraryJobStatus }) {
  const tone =
    status === "queued"
      ? "bg-white/[0.06] text-[var(--fg-2)]"
      : status === "running"
        ? "bg-[var(--amber-400)]/10 text-[var(--amber-300)]"
        : status === "succeeded"
          ? "bg-[var(--success)]/15 text-[var(--success)]"
          : status === "failed"
            ? "bg-[var(--danger)]/15 text-[var(--danger)]"
            : "bg-[var(--warning)]/15 text-[var(--warning)]";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium",
        tone,
      )}
    >
      {status === "running" ? (
        <Spinner size={12} />
      ) : status === "succeeded" ? (
        <CheckCircle2 className="h-3 w-3" />
      ) : status === "failed" || status === "partial" ? (
        <AlertTriangle className="h-3 w-3" />
      ) : null}
      {STATUS_LABEL[status]}
    </span>
  );
}

// amber 渐变进度条；用于运行中和 partial
function ProgressBar({ value }: { value: number }) {
  return (
    <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
      <div
        className="h-full rounded-full bg-gradient-to-r from-[var(--amber-400)] to-[var(--amber-200)] transition-[width] duration-300"
        style={{ width: `${value}%` }}
      />
    </div>
  );
}

function JobThumb({
  item,
  job,
  compact = false,
  disableSaveAction = false,
  onOpenLightbox,
}: {
  item: ApparelModelLibraryJobItem;
  job?: ApparelModelLibraryJob;
  compact?: boolean;
  // dual_race loser 走候选区时关掉「收藏入库」入口
  disableSaveAction?: boolean;
  onOpenLightbox?: () => void;
}) {
  const [saveOpen, setSaveOpen] = useState(false);
  const saved = item.saved_item_id != null;
  const allowSave = !disableSaveAction;
  // 优先取 item 的 appearance；没有就 fallback job 级
  const appearanceKey = (item.appearance_direction || job?.appearance_direction || "") as
    | AppearanceKey
    | "";
  const appearanceLabel = appearanceKey
    ? (MODEL_LIBRARY_APPEARANCE_LABEL[appearanceKey as AppearanceKey] ?? appearanceKey)
    : "";

  return (
    <div
      className={cn(
        "group relative overflow-hidden rounded-xl border bg-[var(--bg-2)] transition-transform duration-200",
        saved
          ? "border-[var(--success)]/40"
          : "border-[var(--border)] hover:border-[var(--border-strong)] group-hover:scale-[1.005]",
      )}
    >
      <button
        type="button"
        onClick={() => onOpenLightbox?.()}
        aria-label="查看大图"
        className={cn(
          "relative block w-full cursor-zoom-in overflow-hidden",
          compact ? "aspect-square" : "aspect-[4/5]",
        )}
      >
        <Image
          src={item.thumb_url || item.image_url}
          alt="生成模特"
          fill
          unoptimized
          sizes="(max-width: 768px) 50vw, 220px"
          className="object-cover transition-transform duration-200 group-hover:scale-[1.015]"
        />
        {saved ? (
          <span className="absolute left-2 top-2 inline-flex items-center gap-1 rounded-md bg-[var(--success)]/85 px-2 py-1 text-[10px] text-white shadow-[var(--shadow-1)] backdrop-blur">
            <Bookmark className="h-3 w-3" />
            已入库
          </span>
        ) : null}
        {/* 右下角 hover 提示：可点开大图 */}
        <span className="pointer-events-none absolute bottom-2 right-2 inline-flex h-7 w-7 items-center justify-center rounded-full bg-black/60 text-white opacity-0 backdrop-blur transition-opacity duration-150 group-hover:opacity-100">
          <Maximize2 className="h-3.5 w-3.5" />
        </span>
      </button>
      {!compact && job && allowSave ? (
        <div className="flex items-center justify-between gap-2 p-2">
          <span className="truncate text-[11px] text-[var(--fg-2)]">
            {[appearanceLabel, item.style_tags.slice(0, 2).join("、")]
              .filter(Boolean)
              .join(" · ") || "未识别风格"}
          </span>
          {!saved ? (
            <>
              {/* 移动端：icon-only 圆按钮 */}
              <button
                type="button"
                aria-label="收藏入库"
                onClick={() => setSaveOpen(true)}
                className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-[var(--accent)] text-[var(--bg-0)] transition-colors hover:bg-[var(--amber-200)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:hidden"
              >
                <Bookmark className="h-3.5 w-3.5" />
              </button>
              {/* 桌面端：带文字 */}
              <Button
                size="sm"
                variant="primary"
                onClick={() => setSaveOpen(true)}
                leftIcon={<Bookmark className="h-3.5 w-3.5" />}
                className="hidden md:inline-flex"
              >
                收藏
              </Button>
            </>
          ) : (
            <span className="rounded-full border border-[var(--success)]/40 bg-[var(--success-soft)] px-2 py-0.5 text-[10px] text-[var(--success)]">
              已入库
            </span>
          )}
        </div>
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
  // appearance 改 chip 选择：空 = 不指定
  const [appearance, setAppearance] = useState<ModelLibraryAppearance | "">(
    () => (item.appearance_direction || job.appearance_direction || "") as ModelLibraryAppearance | "",
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
      className="fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/60 backdrop-blur-md md:items-center md:p-5"
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
        className="flex max-h-[92dvh] w-full flex-col overflow-hidden rounded-t-2xl border border-[var(--border)] bg-[var(--bg-0)] shadow-[var(--shadow-2)] md:max-w-md md:rounded-xl"
      >
        <header className="shrink-0 px-4 pt-4 pb-3">
          <h3 className="font-display text-lg italic text-[var(--fg-0)]">收藏入库</h3>
          <p className="mt-1 text-xs text-[var(--fg-2)]">
            {`填好后会作为"生成入库"模特保存到我的模特库。`}
          </p>
        </header>
        <div className="grid min-h-0 flex-1 gap-3 overflow-y-auto px-4 pb-3">
          <Input
            label="名称"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="高级简洁青年女模特"
          />
          <div className="grid gap-2 md:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-[var(--fg-1)]">年龄段</span>
              <select
                value={age}
                onChange={(event) => setAge(event.target.value as ModelLibraryItemAgeSegment)}
                className="h-11 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none md:h-9"
              >
                {(Object.keys(AGE_LABEL) as ModelLibraryItemAgeSegment[]).map((segment) => (
                  <option key={segment} value={segment}>
                    {AGE_LABEL[segment]}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-[var(--fg-1)]">性别</span>
              <select
                value={gender}
                onChange={(event) => setGender(event.target.value)}
                className="h-11 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none md:h-9"
              >
                <option value="female">女</option>
                <option value="male">男</option>
              </select>
            </label>
          </div>
          {/* 外貌方向：chip 选择，10 + 不指定 */}
          <div className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-[var(--fg-1)]">外貌方向</span>
            <div className="flex flex-wrap gap-1.5">
              <Chip active={appearance === ""} onClick={() => setAppearance("")}>
                不指定
              </Chip>
              {(Object.entries(MODEL_LIBRARY_APPEARANCE_LABEL) as [
                Exclude<ModelLibraryAppearance, "all">,
                string,
              ][]).map(([value, label]) => (
                <Chip
                  key={value}
                  active={appearance === value}
                  onClick={() => setAppearance(value)}
                >
                  {label}
                </Chip>
              ))}
            </div>
          </div>
          <Input
            label="风格标签"
            value={styleTags}
            onChange={(event) => setStyleTags(event.target.value)}
            placeholder="高级简洁、棚拍"
          />
          <label className="flex items-center gap-2 text-xs text-[var(--fg-1)]">
            <input
              type="checkbox"
              checked={autoTag}
              onChange={(event) => setAutoTag(event.target.checked)}
            />
            入库后再跑一次自动识别
          </label>
        </div>
        <footer className="flex shrink-0 justify-end gap-2 border-t border-[var(--border)] px-4 py-3 pb-[calc(0.75rem+env(safe-area-inset-bottom,0px))] md:pb-3">
          <Button variant="ghost" onClick={onClose}>
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

// 复用：和 Generator 里的 Chip 同结构
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
        "inline-flex min-h-11 cursor-pointer items-center rounded-md border px-3 text-xs transition-colors md:min-h-9",
        active
          ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
          : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/[0.04] hover:text-[var(--fg-0)]",
      )}
    >
      {children}
    </button>
  );
}

function EmptyJobs() {
  // 当前面板内拿不到 setTab，跳转留 TODO；先做视觉占位
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-[var(--border)] bg-[var(--bg-1)]/50 px-6 py-14 text-center">
      <div
        aria-hidden
        className="mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-gradient-to-tr from-[var(--amber-400)]/30 to-[var(--amber-200)]/10 shadow-[var(--shadow-amber)]"
      >
        <Library className="h-6 w-6 text-[var(--amber-300)]" />
      </div>
      <h4 className="font-display text-xl italic text-[var(--fg-0)]">还没有任务</h4>
      <p className="mt-2 max-w-sm text-xs leading-5 text-[var(--fg-2)]">
        {`从"新建模特"提交一批，或者在项目里生成模特候选，都会在这里实时聚合。`}
      </p>
      {/* TODO: tab 跳转需要 props 通讯，先静态展示 */}
      <span className="mt-4 inline-flex cursor-default items-center gap-1.5 text-xs font-medium text-[var(--amber-300)]">
        去新建模特
      </span>
    </div>
  );
}
