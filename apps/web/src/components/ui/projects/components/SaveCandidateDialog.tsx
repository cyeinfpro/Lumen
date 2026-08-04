"use client";

import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Dialog } from "@/components/ui/primitives/Dialog";
import { Input } from "@/components/ui/primitives/Input";
import { Select } from "@/components/ui/primitives/Select";
import { toast } from "@/components/ui/primitives/Toast";
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
import { cn } from "@/lib/utils";
import { inferAgeSegmentFromText } from "../utils";

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
  const [title, setTitle] = useState("");
  const [ageSegment, setAgeSegment] = useState<ModelLibraryItemAgeSegment>(
    defaultAgeSegment(workflow),
  );
  const [gender, setGender] = useState<ModelLibraryGender>("female");
  const [appearance, setAppearance] = useState<ModelLibraryAppearance | "">("");
  const [manualTags, setManualTags] = useState(false);
  const [tags, setTags] = useState("");
  const save = useSaveModelCandidateToLibraryMutation(
    workflow.id,
    candidate?.id ?? "",
    {
      onSuccess: () => {
        toast.success("已收藏到模特库");
        onOpenChange(false);
      },
      onError: (err) =>
        toast.error("收藏失败", {
          description: err instanceof Error ? err.message : "稍后重试",
        }),
    },
  );

  const submit = () => {
    if (!candidate) return;
    const finalTitle = title.trim() || `方案 ${candidate.candidate_index}`;
    save.mutate({
      title: finalTitle,
      age_segment: ageSegment,
      gender,
      appearance_direction: appearance || null,
      style_tags: manualTags ? splitTags(tags) : [],
    });
  };

  return (
    <Dialog
      open={open}
      onClose={() => onOpenChange(false)}
      aria-label="收藏到模特库"
      aria-busy={save.isPending}
      className="max-w-lg"
    >
      <Dialog.Header>
        <p className="type-caption">模特库</p>
        <h2 className="type-section-title mt-1">收藏到模特库</h2>
      </Dialog.Header>
      <Dialog.Body>
        <div className="min-w-0 divide-y divide-[var(--border-subtle)]">
          <Field label="名称">
            <Input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder={`方案 ${candidate?.candidate_index ?? ""}`}
            />
          </Field>

          <Field label="年龄段">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
              <Select
                value={ageSegment}
                onChange={(event) =>
                  setAgeSegment(
                    event.target.value as ModelLibraryItemAgeSegment,
                  )
                }
              >
                {AGE_OPTIONS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
              <p className="type-caption min-w-0 break-all text-[var(--fg-2)]">
                {AGE_FOLDER_BY_SEGMENT[ageSegment]}/{gender}
              </p>
            </div>
          </Field>

          <Field label="性别">
            <div className="flex flex-wrap gap-2">
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

          <Field label="外貌方向">
            <div className="flex flex-wrap gap-2">
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

          <Field label="气质方向">
            <div className="flex flex-wrap gap-2">
              <Chip active={!manualTags} onClick={() => setManualTags(false)}>
                不填
              </Chip>
              <Chip active={manualTags} onClick={() => setManualTags(true)}>
                手动填写
              </Chip>
            </div>
            {manualTags ? (
              <Input
                value={tags}
                onChange={(event) => setTags(event.target.value)}
                placeholder="知性通勤、清冷高级"
                wrapperClassName="mt-3"
              />
            ) : null}
          </Field>
        </div>
      </Dialog.Body>
      <Dialog.Footer>
        <Button
          variant="ghost"
          onClick={() => onOpenChange(false)}
          disabled={save.isPending}
        >
          取消
        </Button>
        <Button
          variant="primary"
          loading={save.isPending}
          onClick={submit}
          disabled={!candidate}
        >
          收藏
        </Button>
      </Dialog.Footer>
    </Dialog>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <section className="min-w-0 py-4 first:pt-0 last:pb-0">
      <p className="type-label mb-2 text-[var(--fg-1)]">{label}</p>
      <div className="min-w-0">{children}</div>
    </section>
  );
}

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
        "type-caption inline-flex min-h-9 items-center rounded-[var(--radius-control)] border px-3 py-1.5 transition-colors",
        active
          ? "border-accent-border bg-accent-soft text-accent"
          : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:border-[var(--border-strong)] hover:text-[var(--fg-0)]",
      )}
      aria-pressed={active || undefined}
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
  return inferAgeSegmentFromText(workflow.user_prompt) ?? "user_favorites";
}

function splitTags(value: string): string[] {
  return value
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 12);
}
