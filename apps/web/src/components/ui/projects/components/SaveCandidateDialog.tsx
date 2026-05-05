"use client";

// Editorial 收藏到模特库表单：
// - 不再用 BookmarkPlus 大图标做 prefix；改为 mono eyebrow + serif italic title
// - input/select 走 underline 极简（h-10 + border-b），去 h-9 rounded-md bg-[var(--bg-1)]
// - chip 去 amber-soft 填充，改为选中 amber 文字 + amber 下划线
// - 表单 section 之间用 hairline 分隔

import { useState } from "react";

import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { toast } from "@/components/ui/primitives/Toast";
import { cn } from "@/lib/utils";
import type {
  ModelCandidate,
  ModelLibraryAppearance,
  ModelLibraryItemAgeSegment,
  WorkflowRun,
} from "@/lib/apiClient";
import {
  MODEL_LIBRARY_APPEARANCE_LABEL,
  MODEL_LIBRARY_APPEARANCE_SELECT_OPTIONS,
} from "@/lib/apiClient";
import { useSaveModelCandidateToLibraryMutation } from "@/lib/queries";

const AGE_OPTIONS: Array<[ModelLibraryItemAgeSegment, string]> = [
  ["user_favorites", "用户收藏"],
  ["toddler", "幼儿"],
  ["child", "儿童"],
  ["teen", "青少年"],
  ["young_adult", "青年"],
  ["adult", "熟龄"],
  ["middle_aged", "中年"],
  ["senior", "老年"],
];

const AGE_FOLDER_BY_SEGMENT: Record<ModelLibraryItemAgeSegment, string> = {
  user_favorites: "00_user_favorites",
  toddler: "01_toddler",
  child: "02_child",
  teen: "03_teen",
  young_adult: "04_young_adult",
  adult: "05_adult",
  middle_aged: "06_middle_aged",
  senior: "07_senior",
};

type ModelLibraryGender = "female" | "male";

const GENDER_OPTIONS: Array<[ModelLibraryGender, string]> = [
  ["female", "女"],
  ["male", "男"],
];

interface SaveCandidateDialogProps {
  workflow: WorkflowRun;
  candidate: ModelCandidate | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function SaveCandidateDialog({
  workflow,
  candidate,
  open,
  onOpenChange,
}: SaveCandidateDialogProps) {
  // state 重置由父组件用 key 强制 re-mount 完成（React 19 不允许 effect 中
  // setState）；初始值就是空表单 + 推断出的年龄段。
  const [title, setTitle] = useState("");
  const [ageSegment, setAgeSegment] = useState<ModelLibraryItemAgeSegment>(
    defaultAgeSegment(workflow),
  );
  const [gender, setGender] = useState<ModelLibraryGender>("female");
  // chip 选择：空 = 不指定
  const [appearance, setAppearance] = useState<ModelLibraryAppearance | "">("");
  const [tagsEnabled, setTagsEnabled] = useState(false);
  const [tags, setTags] = useState("");
  const save = useSaveModelCandidateToLibraryMutation(workflow.id, candidate?.id ?? "", {
    onSuccess: () => {
      toast.success("已收藏到模特库");
      onOpenChange(false);
    },
    onError: (err) =>
      toast.error("收藏失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  const titlePlaceholder = `方案 ${candidate?.candidate_index ?? ""}`;

  const form = (
    <div className="-mx-1">
      <Field eyebrow="Name" label="名称">
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder={titlePlaceholder}
          className={UNDERLINE_INPUT}
        />
      </Field>

      <Field eyebrow="Segment" label="年龄段">
        <div className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
          <select
            value={ageSegment}
            onChange={(event) => setAgeSegment(event.target.value as ModelLibraryItemAgeSegment)}
            className={UNDERLINE_INPUT}
          >
            {AGE_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
            <span className="text-[var(--fg-3)]">→ </span>
            {AGE_FOLDER_BY_SEGMENT[ageSegment]}/{gender}
          </p>
        </div>
      </Field>

      <Field eyebrow="Gender" label="性别">
        <div className="flex flex-wrap gap-x-5 gap-y-1.5">
          {GENDER_OPTIONS.map(([value, label]) => (
            <Chip
              key={value}
              active={gender === value}
              onClick={() => setGender(value)}
            >
              {label}
            </Chip>
          ))}
        </div>
      </Field>

      <Field eyebrow="Appearance" label="外貌方向">
        <div className="flex flex-wrap gap-x-5 gap-y-1.5">
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
      </Field>

      <Field eyebrow="Tags" label="气质方向">
        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
          <Chip active={!tagsEnabled} onClick={() => setTagsEnabled(false)}>
            不填
          </Chip>
          <Chip active={tagsEnabled} onClick={() => setTagsEnabled(true)}>
            填写气质
          </Chip>
        </div>
        {tagsEnabled ? (
          <input
            value={tags}
            onChange={(event) => setTags(event.target.value)}
            placeholder="知性通勤、清冷高级"
            className={cn(UNDERLINE_INPUT, "mt-3")}
          />
        ) : null}
      </Field>
    </div>
  );

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={
        <span className="block">
          <span className="block font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-2)]">
            Bookmark · Model Library
          </span>
          <span className="mt-1 block text-[20px] font-semibold leading-[1.2] tracking-tight text-[var(--fg-0)]">
            收藏到模特库
          </span>
        </span>
      }
      description={form}
      confirmText="收藏"
      confirming={save.isPending}
      onConfirm={async () => {
        if (!candidate) return;
        const finalTitle = title.trim() || `方案 ${candidate.candidate_index}`;
        save.mutate({
          title: finalTitle,
          age_segment: ageSegment,
          gender,
          appearance_direction: appearance || null,
          style_tags: tagsEnabled ? splitTags(tags) : [],
        });
      }}
    />
  );
}

const UNDERLINE_INPUT =
  "h-10 w-full border-b border-[var(--border)] bg-transparent px-0 text-[14px] text-[var(--fg-0)] placeholder:text-[var(--fg-3)] " +
  "transition-colors duration-150 focus:border-[var(--border-amber)] focus:outline-none " +
  "max-sm:text-[16px]";

// editorial Field 容器：mono uppercase eyebrow + 中文小标 + hairline 分隔
function Field({
  eyebrow,
  label,
  children,
}: {
  eyebrow: string;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <section className="border-t border-[var(--border)] px-1 py-3.5 first:border-t-0 first:pt-1">
      <header className="mb-2 flex items-baseline justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          {eyebrow}
        </p>
        <p className="text-[11px] text-[var(--fg-2)]">{label}</p>
      </header>
      {children}
    </section>
  );
}

// editorial chip：mono uppercase + dot + 选中 amber 文字 + amber 下划线
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
        "group inline-flex min-h-9 cursor-pointer items-center gap-1.5 border-b py-1.5 font-mono text-[10px] uppercase tracking-[0.16em] transition-colors",
        active
          ? "border-[var(--border-amber)] text-[var(--amber-300)]"
          : "border-transparent text-[var(--fg-1)] hover:text-[var(--fg-0)]",
      )}
      aria-pressed={active || undefined}
    >
      <span
        aria-hidden
        className={cn(
          "h-1 w-1 rounded-full transition-colors",
          active ? "bg-[var(--amber-400)]" : "bg-[var(--fg-3)] group-hover:bg-[var(--fg-1)]",
        )}
      />
      {children}
    </button>
  );
}

function defaultAgeSegment(workflow: WorkflowRun): ModelLibraryItemAgeSegment {
  const profile = workflow.metadata_jsonb?.model_profile;
  if (profile && typeof profile === "object" && "age_segment" in profile) {
    const value = (profile as { age_segment?: unknown }).age_segment;
    if (
      typeof value === "string" &&
      AGE_OPTIONS.some(([option]) => option === value)
    ) {
      return value as ModelLibraryItemAgeSegment;
    }
  }
  const text = workflow.user_prompt;
  if (text.includes("幼儿")) return "toddler";
  if (["儿童", "童装", "小朋友", "孩子"].some((word) => text.includes(word))) return "child";
  if (text.includes("青少年")) return "teen";
  if (text.includes("青年")) return "young_adult";
  if (text.includes("中年") || text.includes("中老年")) return "middle_aged";
  if (text.includes("老年")) return "senior";
  if (text.includes("熟龄") || text.includes("成年")) return "adult";
  return "user_favorites";
}

function splitTags(value: string): string[] {
  return value
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 12);
}
