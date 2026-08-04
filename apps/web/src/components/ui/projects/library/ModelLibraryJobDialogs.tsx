"use client";

// Editorial 重构：杂志大标题 + hairline section + portrait thumb + 去三层卡。
// 任务中心：展示用户所有 apparel-model-library job
// - origin=library_generate（独立生成）
// - origin=project_candidate（项目里调 useCreateModelCandidatesMutation 派发的）

import { motion } from "framer-motion";
import { X } from "lucide-react";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Select } from "@/components/ui/primitives/Select";
import { Switch } from "@/components/ui/primitives/Switch";
import { toast } from "@/components/ui/primitives/Toast";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";
import { cn } from "@/lib/utils";
import type {
  ApparelModelLibraryJob,
  ApparelModelLibraryJobItem,
  ApparelModelLibrarySaveJobItemIn,
  ModelLibraryAppearance,
  ModelLibraryItemAgeSegment,
} from "@/lib/apiClient";
import {
  MODEL_LIBRARY_APPEARANCE_LABEL,
  MODEL_LIBRARY_APPEARANCE_SELECT_OPTIONS,
} from "@/lib/apiClient";
import { useSaveApparelModelLibraryJobItemMutation } from "@/lib/queries";
import { AGE_LABEL } from "./ModelLibraryJobsModel";

const ORIGIN_LABEL: Record<"library_generate" | "project_candidate", string> = {
  library_generate: "独立生成",
  project_candidate: "项目候选",
};

export function SaveJobItemDialog({
  item,
  job,
  onClose,
}: {
  item: ApparelModelLibraryJobItem;
  job: ApparelModelLibraryJob;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const defaultAge: ModelLibraryItemAgeSegment = job.age_segment ?? "young_adult";
  const rawDefaultGender = item.gender || job.gender || "female";
  const defaultGender = rawDefaultGender === "male" ? "male" : "female";
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
          description: err instanceof Error ? err.message : "稍后重试",
        }),
    },
  );

  useBodyScrollLock(true);
  const onDialogKeyDown = useModalLayer({
    open: true,
    rootRef: dialogRef,
    onClose,
  });

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
      className="mobile-dialog-shell mobile-perf-surface fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-[var(--surface-scrim)] md:items-center md:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <motion.div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="收藏入库"
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
        initial={{ opacity: 0, y: 24, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 12, scale: 0.98 }}
        transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
        className="mobile-dialog-panel flex w-full flex-col overflow-hidden border border-[var(--border)] bg-[var(--bg-0)] md:max-h-[92dvh] md:max-w-md"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] px-5 pb-4 pt-5">
          <div>
            <p className="type-page-kicker">
              收藏入库
            </p>
            <h3 className="type-section-title mt-2">
              收藏入库
            </h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-11 w-11 cursor-pointer items-center justify-center text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)] md:h-9 md:w-9"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="mobile-dialog-scroll grid min-h-0 flex-1 gap-5 overflow-y-auto overscroll-contain px-5 py-5">
          <UnderlineLabeled label="名称">
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="高级简洁青年女模特"
              className="control-shell type-body h-11 w-full px-3 text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-accent-border focus:shadow-[var(--ring)] md:h-10"
            />
          </UnderlineLabeled>
          <div className="grid gap-5 md:grid-cols-2">
            <UnderlineLabeled label="年龄段">
              <Select
                value={age}
                onChange={(event) => setAge(event.target.value as ModelLibraryItemAgeSegment)}
              >
                {(Object.keys(AGE_LABEL) as ModelLibraryItemAgeSegment[]).map((segment) => (
                  <option key={segment} value={segment} className="bg-[var(--bg-0)]">
                    {AGE_LABEL[segment]}
                  </option>
                ))}
              </Select>
            </UnderlineLabeled>
            <UnderlineLabeled label="性别">
              <Select
                value={gender}
                onChange={(event) => setGender(event.target.value)}
              >
                <option value="female" className="bg-[var(--bg-0)]">女</option>
                <option value="male" className="bg-[var(--bg-0)]">男</option>
              </Select>
            </UnderlineLabeled>
          </div>
          <UnderlineLabeled label="外貌方向">
            <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1">
              <Chip active={appearance === ""} onClick={() => setAppearance("")}>
                不指定
              </Chip>
              {MODEL_LIBRARY_APPEARANCE_SELECT_OPTIONS.map((value) => (
                <Chip
                  key={value}
                  active={appearance === value}
                  onClick={() => setAppearance(value)}
                >
                  {MODEL_LIBRARY_APPEARANCE_LABEL[value]}
                </Chip>
              ))}
            </div>
          </UnderlineLabeled>
          <UnderlineLabeled label="气质方向">
            <input
              value={styleTags}
              onChange={(event) => setStyleTags(event.target.value)}
              placeholder="知性通勤、清冷高级"
              className="control-shell type-body h-11 w-full px-3 text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-accent-border focus:shadow-[var(--ring)] md:h-10"
            />
          </UnderlineLabeled>
          <div className="control-shell flex min-h-11 items-center gap-3 px-3 type-caption text-[var(--fg-1)]">
            <Switch
              checked={autoTag}
              onCheckedChange={setAutoTag}
              aria-label="入库后自动识别"
            />
            入库后再跑一次自动识别
          </div>
        </div>
        <footer className="mobile-dialog-footer grid shrink-0 grid-cols-1 gap-2 border-t border-[var(--border)] px-5 py-4 min-[380px]:grid-cols-2 md:flex md:justify-end">
          <Button variant="outline" onClick={onClose} className="w-full md:w-auto">
            取消
          </Button>
          <Button variant="primary" loading={save.isPending} onClick={submit} className="w-full md:w-auto">
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
      <span className="type-caption text-[var(--fg-2)]">
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
        "group relative inline-flex min-h-11 cursor-pointer items-center px-1 py-1.5 type-caption transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:shadow-[var(--ring)] md:min-h-9",
        active ? "text-[var(--fg-0)]" : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
      )}
    >
      <span>{children}</span>
      <span
        aria-hidden
        className={cn(
          "absolute inset-x-1 -bottom-px h-px transition-colors duration-[var(--dur-base)]",
          active
            ? "bg-accent"
            : "bg-transparent group-hover:bg-[var(--border-strong)]",
        )}
      />
    </button>
  );
}
