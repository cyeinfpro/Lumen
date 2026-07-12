"use client";

import { AnimatePresence, motion } from "framer-motion";
import { ImagePlus, X } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import {
  MODEL_LIBRARY_APPEARANCE_LABEL,
  MODEL_LIBRARY_APPEARANCE_SELECT_OPTIONS,
  type ModelLibraryAgeSegment,
  type ModelLibraryAppearance,
  type ModelLibraryItemAgeSegment,
} from "@/lib/apiClient";
import {
  useCreateApparelModelLibraryItemMutation,
  useUploadImageMutation,
} from "@/lib/queries";
import { cn } from "@/lib/utils";

import { Chip } from "./ModelLibraryBrowserView";
import {
  AGE_FOLDER_BY_SEGMENT,
  AGE_TABS,
  APPEARANCE_TABS,
  GENDER_OPTIONS,
  SOURCE_FILTERS,
  type BrowserSource,
  type ModelLibraryGender,
} from "./modelLibraryBrowserOptions";

interface EmbeddedModelLibraryMetadata {
  age_segment?: unknown;
  gender?: unknown;
  appearance_direction?: unknown;
  style_tags?: unknown;
}

function embeddedModelLibraryMetadata(image: {
  metadata_jsonb?: Record<string, unknown> | null;
}): EmbeddedModelLibraryMetadata | null {
  const raw = image.metadata_jsonb?.model_library;
  return raw && typeof raw === "object"
    ? (raw as EmbeddedModelLibraryMetadata)
    : null;
}

function isModelLibraryItemAgeSegment(
  value: unknown,
): value is ModelLibraryItemAgeSegment {
  return (
    typeof value === "string" &&
    AGE_TABS.some(([option]) => option !== "all" && option === value)
  );
}

function isModelLibraryGender(value: unknown): value is ModelLibraryGender {
  return value === "female" || value === "male";
}

function isSelectableAppearance(
  value: unknown,
): value is Exclude<ModelLibraryAppearance, "all"> {
  return (
    typeof value === "string" &&
    value !== "all" &&
    value in MODEL_LIBRARY_APPEARANCE_LABEL
  );
}

function styleTagsFromMetadata(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((tag): tag is string => typeof tag === "string")
    .slice(0, 12);
}

interface UploadFormState {
  title: string;
  age_segment: ModelLibraryItemAgeSegment;
  gender: ModelLibraryGender;
  appearance_direction: Exclude<ModelLibraryAppearance, "all"> | "";
  style_tags: string;
}

export function ModelLibraryBrowserOverlays({
  ageSegment,
  appearance,
  defaultAgeSegment,
  mobileFilterOpen,
  source,
  uploadOpen,
  onAgeChange,
  onAppearanceChange,
  onCloseMobileFilter,
  onCloseUpload,
  onCreated,
  onSourceChange,
}: {
  ageSegment: ModelLibraryAgeSegment;
  appearance: ModelLibraryAppearance;
  defaultAgeSegment: ModelLibraryAgeSegment;
  mobileFilterOpen: boolean;
  source: BrowserSource;
  uploadOpen: boolean;
  onAgeChange: (value: ModelLibraryAgeSegment) => void;
  onAppearanceChange: (value: ModelLibraryAppearance) => void;
  onCloseMobileFilter: () => void;
  onCloseUpload: () => void;
  onCreated: (id: string) => void;
  onSourceChange: (value: BrowserSource) => void;
}) {
  return (
    <>
      <AnimatePresence>
        {uploadOpen ? (
          <UploadDialog
            key="upload-dialog"
            defaultAgeSegment={defaultAgeSegment}
            onClose={onCloseUpload}
            onCreated={onCreated}
          />
        ) : null}
      </AnimatePresence>

      <AnimatePresence>
        {mobileFilterOpen ? (
          <MobileFilterSheet
            key="mobile-filter"
            ageSegment={ageSegment}
            appearance={appearance}
            source={source}
            onAgeChange={onAgeChange}
            onAppearanceChange={onAppearanceChange}
            onSourceChange={onSourceChange}
            onClose={onCloseMobileFilter}
          />
        ) : null}
      </AnimatePresence>
    </>
  );
}

function UploadDialog({
  defaultAgeSegment,
  onClose,
  onCreated,
}: {
  defaultAgeSegment: ModelLibraryAgeSegment;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [form, setForm] = useState<UploadFormState>({
    title: "",
    age_segment:
      defaultAgeSegment === "all" ? "user_favorites" : defaultAgeSegment,
    gender: "female",
    appearance_direction: "",
    style_tags: "",
  });
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadTagsEnabled, setUploadTagsEnabled] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const uploadImage = useUploadImageMutation();
  const createItem = useCreateApparelModelLibraryItemMutation({
    onSuccess: (item) => {
      toast.success("已加入我的模特库");
      onCreated(item.id);
      onClose();
    },
    onError: (err) =>
      toast.error("登记模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  useBodyScrollLock(true);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const submit = async () => {
    if (!uploadFile) {
      toast.warning("未选模特图");
      return;
    }
    const title = form.title.trim() || uploadFile.name.replace(/\.[^.]+$/, "");
    let uploaded;
    try {
      uploaded = await uploadImage.mutateAsync(uploadFile);
    } catch (error) {
      toast.error("上传模特图失败", {
        description: error instanceof Error ? error.message : "请稍后重试",
      });
      return;
    }
    const embedded = embeddedModelLibraryMetadata(uploaded);
    const embeddedTags = styleTagsFromMetadata(embedded?.style_tags);
    const ageSegment =
      embedded && isModelLibraryItemAgeSegment(embedded.age_segment)
        ? embedded.age_segment
        : form.age_segment;
    const gender =
      embedded && isModelLibraryGender(embedded.gender)
        ? embedded.gender
        : form.gender;
    const appearanceDirection =
      embedded && isSelectableAppearance(embedded.appearance_direction)
        ? embedded.appearance_direction
        : form.appearance_direction || null;
    createItem.mutate({
      source: "user_upload",
      image_id: uploaded.id,
      title,
      age_segment: ageSegment,
      gender,
      appearance_direction: appearanceDirection,
      style_tags: uploadTagsEnabled ? splitTags(form.style_tags) : embeddedTags,
    });
  };

  const submitting = uploadImage.isPending || createItem.isPending;

  return (
    <div
      className="mobile-dialog-shell mobile-perf-surface fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/60 backdrop-blur-md md:items-center md:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label="上传到模特库"
        initial={{ opacity: 0, y: 24, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 16, scale: 0.98 }}
        transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
        className="mobile-dialog-panel flex w-full flex-col overflow-hidden border border-[var(--border)] bg-[var(--bg-0)] md:max-h-[92dvh] md:max-w-2xl"
      >
        <header className="flex items-start justify-between gap-3 border-b border-[var(--border)] px-5 pb-4 pt-5">
          <div>
            <p className="type-page-kicker">上传到模特库</p>
            <h3 className="type-page-title mt-2 md:text-[28px]">
              上传到模特库
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

        <div className="mobile-dialog-scroll grid min-h-0 flex-1 gap-5 overflow-y-auto overscroll-contain px-5 py-5 md:grid-cols-2">
          <UnderlineLabeled label="名称" wrapperClass="md:col-span-2">
            <input
              value={form.title}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, title: event.target.value }))
              }
              placeholder="我的高级简洁女模特"
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            />
          </UnderlineLabeled>
          <UnderlineLabeled label="年龄段">
            <select
              value={form.age_segment}
              onChange={(event) =>
                setForm((prev) => ({
                  ...prev,
                  age_segment: event.target.value as ModelLibraryItemAgeSegment,
                }))
              }
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            >
              {AGE_TABS.filter(([value]) => value !== "all").map(
                ([value, label]) => (
                  <option
                    key={value}
                    value={value}
                    className="bg-[var(--bg-0)]"
                  >
                    {label}
                  </option>
                ),
              )}
            </select>
          </UnderlineLabeled>
          <UnderlineLabeled label="性别">
            <select
              value={form.gender}
              onChange={(event) =>
                setForm((prev) => ({
                  ...prev,
                  gender: event.target.value as ModelLibraryGender,
                }))
              }
              className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none focus:border-[var(--amber-400)] md:h-10 md:text-sm"
            >
              {GENDER_OPTIONS.map(([value, label]) => (
                <option key={value} value={value} className="bg-[var(--bg-0)]">
                  {label}
                </option>
              ))}
            </select>
          </UnderlineLabeled>
          <div className="md:col-span-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              Target folder
            </p>
            <p className="mt-1.5 border-b border-[var(--border)] py-2 font-mono text-[12px] text-[var(--fg-1)]">
              {AGE_FOLDER_BY_SEGMENT[form.age_segment]}/{form.gender}
            </p>
          </div>
          <UnderlineLabeled
            label="外貌方向（可选）"
            wrapperClass="md:col-span-2"
          >
            <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1">
              <Chip
                active={form.appearance_direction === ""}
                onClick={() =>
                  setForm((prev) => ({ ...prev, appearance_direction: "" }))
                }
              >
                未指定
              </Chip>
              {MODEL_LIBRARY_APPEARANCE_SELECT_OPTIONS.map((value) => (
                <Chip
                  key={value}
                  active={form.appearance_direction === value}
                  onClick={() =>
                    setForm((prev) => ({
                      ...prev,
                      appearance_direction: value,
                    }))
                  }
                >
                  {MODEL_LIBRARY_APPEARANCE_LABEL[value]}
                </Chip>
              ))}
            </div>
          </UnderlineLabeled>
          <UnderlineLabeled label="气质方向">
            <button
              type="button"
              onClick={() => setUploadTagsEnabled((value) => !value)}
              className="group flex h-11 w-full items-center gap-3 border-b border-[var(--border)] px-1 text-left transition-colors hover:border-[var(--border-strong)] md:h-10"
              aria-pressed={uploadTagsEnabled}
            >
              <span
                aria-hidden
                className={cn(
                  "inline-flex h-4 w-7 shrink-0 items-center rounded-full border transition-colors",
                  uploadTagsEnabled
                    ? "border-[var(--border-amber)] bg-[var(--accent)]"
                    : "border-[var(--border-strong)] bg-transparent",
                )}
              >
                <span
                  className={cn(
                    "ml-0.5 h-3 w-3 rounded-full bg-white transition-transform",
                    uploadTagsEnabled ? "translate-x-3" : "",
                  )}
                />
              </span>
              <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--fg-1)]">
                {uploadTagsEnabled ? "手动填写" : "自动识别"}
              </span>
            </button>
          </UnderlineLabeled>
          {uploadTagsEnabled ? (
            <UnderlineLabeled label="气质标签">
              <input
                value={form.style_tags}
                onChange={(event) =>
                  setForm((prev) => ({
                    ...prev,
                    style_tags: event.target.value,
                  }))
                }
                placeholder="清冷高级、知性通勤"
                className="h-11 w-full border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
              />
            </UnderlineLabeled>
          ) : (
            <div className="hidden md:block" />
          )}
          <div className="md:col-span-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              模特图
            </p>
            <input
              ref={fileInputRef}
              type="file"
              accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"
              className="hidden"
              onChange={(event) =>
                setUploadFile(event.target.files?.[0] ?? null)
              }
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="mt-1.5 flex min-h-11 w-full items-center gap-3 border-b border-[var(--border)] py-3 text-left transition-colors hover:border-[var(--border-strong)]"
            >
              <ImagePlus className="h-4 w-4 text-[var(--fg-2)]" />
              <span className="truncate text-[14px] text-[var(--fg-0)]">
                {uploadFile ? uploadFile.name : "选图"}
              </span>
            </button>
          </div>
        </div>

        <footer className="mobile-dialog-footer grid shrink-0 grid-cols-2 gap-2 border-t border-[var(--border)] px-5 py-4 md:flex md:items-center md:justify-end">
          <Button
            variant="outline"
            onClick={onClose}
            disabled={submitting}
            className="w-full md:w-auto"
          >
            取消
          </Button>
          <Button
            variant="primary"
            loading={submitting}
            onClick={submit}
            className="w-full md:w-auto"
          >
            加入
          </Button>
        </footer>
      </motion.div>
    </div>
  );
}

function UnderlineLabeled({
  label,
  children,
  wrapperClass,
}: {
  label: string;
  children: ReactNode;
  wrapperClass?: string;
}) {
  return (
    <label className={cn("grid gap-2", wrapperClass)}>
      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        {label}
      </span>
      {children}
    </label>
  );
}

function MobileFilterSheet({
  ageSegment,
  appearance,
  source,
  onAgeChange,
  onAppearanceChange,
  onSourceChange,
  onClose,
}: {
  ageSegment: ModelLibraryAgeSegment;
  appearance: ModelLibraryAppearance;
  source: BrowserSource;
  onAgeChange: (value: ModelLibraryAgeSegment) => void;
  onAppearanceChange: (value: ModelLibraryAppearance) => void;
  onSourceChange: (value: BrowserSource) => void;
  onClose: () => void;
}) {
  useBodyScrollLock(true);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  return (
    <div
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end bg-black/60 backdrop-blur-sm md:hidden"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label="筛选"
        initial={{ y: "100%" }}
        animate={{ y: 0 }}
        exit={{ y: "100%" }}
        transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
        className="mobile-dialog-sheet flex w-full flex-col overflow-hidden border-t border-[var(--border)] bg-[var(--bg-0)]"
      >
        <header className="flex items-start justify-between gap-2 border-b border-[var(--border)] px-5 pb-4 pt-5">
          <div>
            <p className="type-page-kicker">筛选</p>
            <h3 className="type-page-title-sm mt-2">筛选</h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-11 w-11 cursor-pointer items-center justify-center text-[var(--fg-2)] hover:text-[var(--fg-0)]"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="mobile-dialog-scroll flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto overscroll-contain px-5 py-5">
          <div className="grid gap-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              年龄段
            </p>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {AGE_TABS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={ageSegment === value}
                  onClick={() => onAgeChange(value)}
                >
                  {label}
                </Chip>
              ))}
            </div>
          </div>
          <div className="grid gap-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              外貌方向
            </p>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {APPEARANCE_TABS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={appearance === value}
                  onClick={() => onAppearanceChange(value)}
                >
                  {label}
                </Chip>
              ))}
            </div>
          </div>
          <div className="grid gap-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              来源
            </p>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {SOURCE_FILTERS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={source === value}
                  onClick={() => onSourceChange(value)}
                >
                  {label}
                </Chip>
              ))}
            </div>
          </div>
        </div>
        <footer className="mobile-dialog-footer grid shrink-0 grid-cols-2 gap-2 border-t border-[var(--border)] px-5 py-4 md:flex md:items-center md:justify-between">
          <Button
            variant="outline"
            onClick={() => {
              onAgeChange("all");
              onAppearanceChange("all");
              onSourceChange("all");
            }}
            className="w-full md:w-auto"
          >
            清空
          </Button>
          <Button
            variant="primary"
            onClick={onClose}
            className="w-full md:w-auto"
          >
            完成
          </Button>
        </footer>
      </motion.div>
    </div>
  );
}

function splitTags(value: string): string[] {
  return value
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 12);
}
