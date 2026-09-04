"use client";

import { ImagePlus, X } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";

import { Dialog, IconButton } from "@/components/ui/primitives";
import { Button } from "@/components/ui/primitives/Button";
import { Select } from "@/components/ui/primitives/Select";
import { Switch } from "@/components/ui/primitives/Switch";
import { toast } from "@/components/ui/primitives/Toast";
import { BottomSheet } from "@/components/ui/primitives/mobile";
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
  styleTag,
  styleTagOptions,
  uploadOpen,
  onAgeChange,
  onAppearanceChange,
  onCloseMobileFilter,
  onCloseUpload,
  onCreated,
  onSourceChange,
  onStyleTagChange,
}: {
  ageSegment: ModelLibraryAgeSegment;
  appearance: ModelLibraryAppearance;
  defaultAgeSegment: ModelLibraryAgeSegment;
  mobileFilterOpen: boolean;
  source: BrowserSource;
  styleTag: string;
  styleTagOptions: string[];
  uploadOpen: boolean;
  onAgeChange: (value: ModelLibraryAgeSegment) => void;
  onAppearanceChange: (value: ModelLibraryAppearance) => void;
  onCloseMobileFilter: () => void;
  onCloseUpload: () => void;
  onCreated: (id: string) => void;
  onSourceChange: (value: BrowserSource) => void;
  onStyleTagChange: (value: string) => void;
}) {
  return (
    <>
      {uploadOpen ? (
        <UploadDialog
          defaultAgeSegment={defaultAgeSegment}
          onClose={onCloseUpload}
          onCreated={onCreated}
        />
      ) : null}

      {mobileFilterOpen ? (
        <MobileFilterSheet
          ageSegment={ageSegment}
          appearance={appearance}
          source={source}
          styleTag={styleTag}
          styleTagOptions={styleTagOptions}
          onAgeChange={onAgeChange}
          onAppearanceChange={onAppearanceChange}
          onSourceChange={onSourceChange}
          onStyleTagChange={onStyleTagChange}
          onClose={onCloseMobileFilter}
        />
      ) : null}
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
  const [manualTags, setManualTags] = useState(false);
  const nameInputRef = useRef<HTMLInputElement | null>(null);
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
        description: err instanceof Error ? err.message : "稍后重试",
      }),
  });

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
        description: error instanceof Error ? error.message : "稍后重试",
      });
      return;
    }
    const embedded = embeddedModelLibraryMetadata(uploaded);
    const embeddedTags = styleTagsFromMetadata(embedded?.style_tags);
    const appearanceDirection =
      form.appearance_direction ||
      (embedded && isSelectableAppearance(embedded.appearance_direction)
        ? embedded.appearance_direction
        : null);
    createItem.mutate({
      source: "user_upload",
      image_id: uploaded.id,
      title,
      age_segment: form.age_segment,
      gender: form.gender,
      appearance_direction: appearanceDirection,
      style_tags: manualTags ? splitTags(form.style_tags) : embeddedTags,
    });
  };

  const submitting = uploadImage.isPending || createItem.isPending;

  return (
    <Dialog
      open
      onClose={onClose}
      initialFocusRef={nameInputRef}
      aria-label="上传到模特库"
      className="flex max-h-[92dvh] max-w-2xl flex-col"
    >
        <Dialog.Header className="flex items-start justify-between gap-3 px-5 pb-4 pt-5">
          <div>
            <p className="type-page-kicker">上传到模特库</p>
            <h3 className="type-page-title mt-2 ">
              上传到模特库
            </h3>
          </div>
          <IconButton
            type="button"
            onClick={onClose}
            aria-label="关闭"
            tooltip="关闭"
          >
            <X className="h-4 w-4" />
          </IconButton>
        </Dialog.Header>

        <Dialog.Body className="grid min-h-0 flex-1 gap-5 overflow-y-auto overscroll-contain px-5 py-5 md:grid-cols-2">
          <UnderlineLabeled label="名称" wrapperClass="md:col-span-2">
            <input
              ref={nameInputRef}
              value={form.title}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, title: event.target.value }))
              }
              placeholder="我的高级简洁女模特"
              className="control-shell type-body h-11 w-full px-3 text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-accent-border focus:shadow-[var(--ring)] md:h-10"
            />
          </UnderlineLabeled>
          <UnderlineLabeled label="年龄段">
            <Select
              value={form.age_segment}
              onChange={(event) =>
                setForm((prev) => ({
                  ...prev,
                  age_segment: event.target.value as ModelLibraryItemAgeSegment,
                }))
              }
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
            </Select>
          </UnderlineLabeled>
          <UnderlineLabeled label="性别">
            <Select
              value={form.gender}
              onChange={(event) =>
                setForm((prev) => ({
                  ...prev,
                  gender: event.target.value as ModelLibraryGender,
                }))
              }
            >
              {GENDER_OPTIONS.map(([value, label]) => (
                <option key={value} value={value} className="bg-[var(--bg-0)]">
                  {label}
                </option>
              ))}
            </Select>
          </UnderlineLabeled>
          <div className="md:col-span-2">
            <p className="type-caption text-[var(--fg-2)]">
              目标目录
            </p>
            <p className="control-shell mt-1.5 px-3 py-2 type-caption text-[var(--fg-1)]">
              {AGE_FOLDER_BY_SEGMENT[form.age_segment]}/{form.gender}
            </p>
          </div>
          <UnderlineLabeled
            label="外貌方向（可选）"
            wrapperClass="md:col-span-2"
          >
            <div
              className="flex flex-wrap gap-x-4 gap-y-1 pt-1"
              role="group"
              aria-label="上传模特外貌方向"
            >
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
          <div className="grid gap-2">
            <span className="type-caption text-[var(--fg-2)]">气质方向</span>
            <div className="control-shell flex min-h-11 items-center gap-3 px-3">
              <Switch
                checked={manualTags}
                onCheckedChange={setManualTags}
                aria-label="手动填写气质标签"
              />
              <span className="type-caption text-[var(--fg-1)]">
                {manualTags ? "手动填写" : "自动识别"}
              </span>
            </div>
          </div>
          {manualTags ? (
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
                className="control-shell type-body h-11 w-full px-3 text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-accent-border focus:shadow-[var(--ring)] md:h-10"
              />
            </UnderlineLabeled>
          ) : (
            <div className="hidden md:block" />
          )}
          <div className="md:col-span-2">
            <p className="type-caption text-[var(--fg-2)]">
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
              className="control-shell mt-1.5 flex min-h-11 w-full items-center gap-3 px-3 py-2 text-left transition-colors hover:border-[var(--border-strong)]"
            >
              <ImagePlus className="h-4 w-4 text-[var(--fg-2)]" />
              <span className="truncate type-body-sm text-[var(--fg-0)]">
                {uploadFile ? uploadFile.name : "选图"}
              </span>
            </button>
          </div>
        </Dialog.Body>

        <Dialog.Footer className="grid shrink-0 grid-cols-2 gap-2 px-5 py-4 md:flex md:items-center md:justify-end">
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
        </Dialog.Footer>
    </Dialog>
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
      <span className="type-caption text-[var(--fg-2)]">
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
  styleTag,
  styleTagOptions,
  onAgeChange,
  onAppearanceChange,
  onSourceChange,
  onStyleTagChange,
  onClose,
}: {
  ageSegment: ModelLibraryAgeSegment;
  appearance: ModelLibraryAppearance;
  source: BrowserSource;
  styleTag: string;
  styleTagOptions: string[];
  onAgeChange: (value: ModelLibraryAgeSegment) => void;
  onAppearanceChange: (value: ModelLibraryAppearance) => void;
  onSourceChange: (value: BrowserSource) => void;
  onStyleTagChange: (value: string) => void;
  onClose: () => void;
}) {
  return (
    <BottomSheet
      open
      onClose={onClose}
      ariaLabel="筛选"
      snapPoints={["88%"]}
      className="md:hidden"
    >
        <header className="flex items-start justify-between gap-2 border-b border-[var(--border)] px-5 pb-4 pt-5">
          <div>
            <p className="type-page-kicker">筛选</p>
            <h3 className="type-page-title-sm mt-2">筛选</h3>
          </div>
          <IconButton
            type="button"
            onClick={onClose}
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </IconButton>
        </header>
        <div className="mobile-dialog-scroll flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto overscroll-contain px-5 py-5">
          <div className="grid gap-2">
            <p className="type-caption text-[var(--fg-2)]">
              年龄段
            </p>
            <div
              className="flex flex-wrap gap-x-4 gap-y-1"
              role="group"
              aria-label="年龄段"
            >
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
            <p className="type-caption text-[var(--fg-2)]">
              外貌方向
            </p>
            <div
              className="flex flex-wrap gap-x-4 gap-y-1"
              role="group"
              aria-label="外貌方向"
            >
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
            <p className="type-caption text-[var(--fg-2)]">
              气质方向
            </p>
            <div
              className="flex flex-wrap gap-x-4 gap-y-1"
              role="group"
              aria-label="气质方向"
            >
              <Chip
                active={!styleTag}
                onClick={() => onStyleTagChange("")}
              >
                全部
              </Chip>
              {styleTagOptions.map((tag) => (
                <Chip
                  key={tag}
                  active={styleTag === tag}
                  onClick={() => onStyleTagChange(tag)}
                >
                  {tag}
                </Chip>
              ))}
            </div>
          </div>
          <div className="grid gap-2">
            <p className="type-caption text-[var(--fg-2)]">
              来源
            </p>
            <div
              className="flex flex-wrap gap-x-4 gap-y-1"
              role="group"
              aria-label="来源"
            >
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
              onStyleTagChange("");
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
    </BottomSheet>
  );
}

function splitTags(value: string): string[] {
  return value
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 12);
}
