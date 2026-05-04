"use client";

import { BookmarkPlus } from "lucide-react";
import { useState } from "react";

import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { Input } from "@/components/ui/primitives/Input";
import { toast } from "@/components/ui/primitives/Toast";
import { cn } from "@/lib/utils";
import type {
  ModelCandidate,
  ModelLibraryAppearance,
  ModelLibraryItemAgeSegment,
  WorkflowRun,
} from "@/lib/apiClient";
import { MODEL_LIBRARY_APPEARANCE_LABEL } from "@/lib/apiClient";
import { useSaveModelCandidateToLibraryMutation } from "@/lib/queries";

const AGE_OPTIONS: Array<[ModelLibraryItemAgeSegment, string]> = [
  ["user_favorites", "用户收藏"],
  ["toddler", "幼儿"],
  ["child", "儿童"],
  ["teen", "青少年"],
  ["young_adult", "青年"],
  ["adult", "成年"],
  ["middle_aged", "中老年"],
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

  const form = (
    <div className="mt-4 flex flex-col gap-3 text-left">
      <Input
        label="名称"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        placeholder={`方案 ${candidate?.candidate_index ?? ""}`}
      />
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-[var(--fg-1)]">年龄段</span>
          <select
            value={ageSegment}
            onChange={(event) => setAgeSegment(event.target.value as ModelLibraryItemAgeSegment)}
            className="h-9 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none"
          >
            {AGE_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium text-[var(--fg-1)]">目标文件夹</span>
          <div className="flex h-9 items-center rounded-md border border-[var(--border)] bg-black/15 px-3 font-mono text-xs text-[var(--fg-1)]">
            {AGE_FOLDER_BY_SEGMENT[ageSegment]}/{gender}
          </div>
        </div>
      </div>
      <label className="flex flex-col gap-1">
        <span className="text-xs font-medium text-[var(--fg-1)]">性别</span>
        <select
          value={gender}
          onChange={(event) => setGender(event.target.value as ModelLibraryGender)}
          className="h-9 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none"
        >
          {GENDER_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
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
      <label className="flex flex-col gap-1">
        <span className="text-xs font-medium text-[var(--fg-1)]">标签</span>
        <button
          type="button"
          onClick={() => setTagsEnabled((value) => !value)}
          className={cn(
            "h-9 rounded-md border px-3 text-left text-sm transition-colors",
            tagsEnabled
              ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
              : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-1)]",
          )}
        >
          {tagsEnabled ? "填写标签" : "不填标签"}
        </button>
      </label>
      {tagsEnabled ? (
        <Input
          label="标签内容"
          value={tags}
          onChange={(event) => setTags(event.target.value)}
          placeholder="通勤、冷淡、高级简洁"
        />
      ) : null}
    </div>
  );

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={
        <span className="inline-flex items-center gap-2">
          <BookmarkPlus className="h-4 w-4" />
          收藏到模特库
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

// 复用：和 Generator/JobsPanel 里的 Chip 同结构
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
        "inline-flex min-h-9 cursor-pointer items-center rounded-md border px-3 text-xs transition-colors",
        active
          ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
          : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/[0.04] hover:text-[var(--fg-0)]",
      )}
    >
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
  if (text.includes("中老年")) return "middle_aged";
  if (text.includes("老年")) return "senior";
  if (text.includes("成年")) return "adult";
  return "user_favorites";
}

function splitTags(value: string): string[] {
  return value
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 12);
}
