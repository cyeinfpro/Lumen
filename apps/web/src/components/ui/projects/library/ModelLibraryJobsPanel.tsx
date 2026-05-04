"use client";

// 任务中心：展示用户所有 apparel-model-library job
// - origin=library_generate（独立生成）
// - origin=project_candidate（项目里调 useCreateModelCandidatesMutation 派发的）
// 上半部分：进行中（queued / running）
// 下半部分：已完成 / 失败 / 部分成功（succeeded / failed / partial）
//
// 已完成 job 的图：点"收藏入库"会弹一个简表，title / age_segment / gender 等都从 job 字段继承。

import { motion } from "framer-motion";
import {
  AlertTriangle,
  Bookmark,
  CheckCircle2,
  ExternalLink,
  Library,
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
import type {
  ApparelModelLibraryJob,
  ApparelModelLibraryJobItem,
  ApparelModelLibraryJobStatus,
  ApparelModelLibrarySaveJobItemIn,
  ModelLibraryItemAgeSegment,
} from "@/lib/apiClient";
import {
  useApparelModelLibraryJobsQuery,
  useSaveApparelModelLibraryJobItemMutation,
} from "@/lib/queries";
import { formatRelativeTime } from "../utils";

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
          <p className="flex items-center gap-2 text-[11px] font-medium tracking-[0.16em] text-[var(--fg-2)]">
            <Library className="h-3.5 w-3.5" />
            JOBS CENTER
          </p>
          <h3 className="mt-2 text-[20px] font-semibold tracking-normal text-[var(--fg-0)] md:text-[22px]">
            任务中心
          </h3>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
            {`把"独立生成"和"项目里的模特候选"放在一起跟踪。每 5 秒自动刷新。`}
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          loading={jobs.isFetching}
          onClick={() => jobs.refetch()}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          手动刷新
        </Button>
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
      className="grid gap-3 rounded-xl border border-[var(--border-amber)]/60 bg-[var(--accent-soft)]/40 p-3 shadow-[var(--shadow-1)] md:rounded-md"
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Spinner size={16} />
            <span className="text-sm font-medium text-[var(--amber-300)]">
              {STATUS_LABEL[job.status]}
            </span>
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
      <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
        <div
          className="h-full bg-[var(--accent)] transition-[width] duration-300"
          style={{ width: `${progress}%` }}
        />
      </div>
      {job.items.length > 0 ? (
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6">
          {job.items.map((item) => (
            <JobThumb key={item.image_id} item={item} compact />
          ))}
        </div>
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
  const StatusIcon =
    job.status === "succeeded"
      ? CheckCircle2
      : job.status === "failed"
        ? AlertTriangle
        : AlertTriangle;
  return (
    <motion.article
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      className={cn(
        "grid gap-3 rounded-xl border bg-[var(--bg-1)]/72 p-3 shadow-[var(--shadow-1)] md:rounded-md md:bg-white/[0.035]",
        tone,
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <StatusIcon
              className={cn(
                "h-4 w-4",
                job.status === "succeeded"
                  ? "text-[var(--success)]"
                  : job.status === "failed"
                    ? "text-[var(--danger)]"
                    : "text-[var(--amber-300)]",
              )}
            />
            <span className="text-sm font-medium text-[var(--fg-0)]">
              {STATUS_LABEL[job.status]}
            </span>
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
      {job.items.length === 0 ? (
        <p className="rounded-md border border-dashed border-[var(--border)] bg-white/[0.02] px-3 py-4 text-center text-xs text-[var(--fg-2)]">
          没有已落地的图像
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5">
          {job.items.map((item) => (
            <JobThumb key={item.image_id} item={item} job={job} />
          ))}
        </div>
      )}
    </motion.article>
  );
}

function BriefMeta({ job }: { job: ApparelModelLibraryJob }) {
  const tokens: string[] = [];
  if (job.age_segment) tokens.push(AGE_LABEL[job.age_segment]);
  if (job.gender) tokens.push(job.gender);
  if (job.appearance_direction) tokens.push(job.appearance_direction);
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

function JobThumb({
  item,
  job,
  compact = false,
}: {
  item: ApparelModelLibraryJobItem;
  job?: ApparelModelLibraryJob;
  compact?: boolean;
}) {
  const [saveOpen, setSaveOpen] = useState(false);
  const saved = item.saved_item_id != null;

  return (
    <div
      className={cn(
        "group relative overflow-hidden rounded-md border bg-[var(--bg-2)]",
        saved
          ? "border-[var(--success)]/40"
          : "border-[var(--border)] hover:border-[var(--border-strong)]",
      )}
    >
      <div className={cn("relative w-full overflow-hidden", compact ? "aspect-square" : "aspect-[4/5]")}>
        <Image
          src={item.thumb_url || item.image_url}
          alt="生成模特"
          fill
          unoptimized
          sizes="(max-width: 768px) 50vw, 220px"
          className="object-cover transition-transform duration-200 group-hover:scale-[1.015]"
        />
        {saved ? (
          <span className="absolute left-2 top-2 inline-flex items-center gap-1 rounded-md bg-[var(--success)]/90 px-2 py-1 text-[10px] text-white backdrop-blur">
            <Bookmark className="h-3 w-3" />
            已入库
          </span>
        ) : null}
      </div>
      {!compact && job ? (
        <div className="flex items-center justify-between gap-2 p-2">
          {item.style_tags.length > 0 ? (
            <span className="truncate text-[11px] text-[var(--fg-2)]">
              {item.style_tags.slice(0, 2).join("、")}
            </span>
          ) : (
            <span className="text-[11px] text-[var(--fg-2)]">
              {item.appearance_direction || "未识别风格"}
            </span>
          )}
          {!saved ? (
            <Button
              size="sm"
              variant="primary"
              onClick={() => setSaveOpen(true)}
              leftIcon={<Bookmark className="h-3.5 w-3.5" />}
            >
              收藏
            </Button>
          ) : (
            <span className="rounded-full border border-[var(--success)]/40 bg-[var(--success-soft)] px-2 py-0.5 text-[10px] text-[var(--success)]">
              已入库
            </span>
          )}
        </div>
      ) : null}
      {saveOpen && job ? (
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
  const [appearance, setAppearance] = useState(
    item.appearance_direction || job.appearance_direction || "",
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
      appearance_direction: appearance.trim() || null,
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
      className="fixed inset-0 z-[var(--z-dialog)] flex items-center justify-center bg-black/60 p-3 backdrop-blur-md md:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label="收藏入库"
        initial={{ opacity: 0, y: 12, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
        className="grid w-full max-w-md gap-3 rounded-md border border-[var(--border)] bg-[var(--bg-0)] p-4 shadow-[var(--shadow-2)]"
      >
        <header>
          <h3 className="text-base font-semibold text-[var(--fg-0)]">收藏入库</h3>
          <p className="mt-1 text-xs text-[var(--fg-2)]">
            {`填好后会作为"生成入库"模特保存到我的模特库。`}
          </p>
        </header>
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
              className="h-9 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none"
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
              className="h-9 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none"
            >
              <option value="female">女</option>
              <option value="male">男</option>
            </select>
          </label>
        </div>
        <Input
          label="外貌偏向"
          value={appearance}
          onChange={(event) => setAppearance(event.target.value)}
          placeholder="温柔、极简"
        />
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
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" loading={save.isPending} onClick={submit}>
            保存
          </Button>
        </div>
      </motion.div>
    </div>
  );
}

function EmptyJobs() {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-[var(--border)] bg-white/[0.02] px-6 py-12 text-center md:rounded-md">
      <Library className="h-8 w-8 text-[var(--fg-2)]" />
      <p className="mt-3 text-sm font-medium text-[var(--fg-0)]">还没有任务</p>
      <p className="mt-1 max-w-sm text-xs text-[var(--fg-2)]">
        {`切到"新建模特"tab 提交一次生成，或者在项目里调"生成模特候选"，都会在这里聚合。`}
      </p>
    </div>
  );
}
