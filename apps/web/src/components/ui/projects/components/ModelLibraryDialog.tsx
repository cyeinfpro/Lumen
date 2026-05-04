"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  Check,
  Eye,
  ImagePlus,
  Library,
  MoreHorizontal,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  WandSparkles,
  X,
} from "lucide-react";
import Image from "next/image";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Input } from "@/components/ui/primitives/Input";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { toast } from "@/components/ui/primitives/Toast";
import { cn } from "@/lib/utils";
import type {
  ApparelModelLibraryItem,
  ModelLibraryAgeSegment,
  ModelLibraryItemAgeSegment,
  ModelLibrarySource,
  WorkflowRun,
} from "@/lib/apiClient";
import {
  useApparelModelLibraryQuery,
  useCreateApparelModelLibraryItemMutation,
  useDeleteApparelModelLibraryItemMutation,
  useSelectApparelModelLibraryItemMutation,
  useSyncApparelModelLibraryPresetsMutation,
  useUploadImageMutation,
} from "@/lib/queries";
import { formatShortDate } from "../utils";

const AGE_TABS: Array<[ModelLibraryAgeSegment, string]> = [
  ["all", "全部"],
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

const SOURCE_FILTERS: Array<["all" | ModelLibrarySource, string]> = [
  ["all", "全部"],
  ["preset", "全站预设"],
  ["favorite", "我的收藏"],
  ["user_upload", "我的上传"],
];

const SOURCE_LABEL: Record<ModelLibrarySource, string> = {
  preset: "全站预设",
  favorite: "我的收藏",
  user_upload: "我的上传",
};

const AGE_LABEL = Object.fromEntries(AGE_TABS) as Record<ModelLibraryAgeSegment, string>;

interface ModelLibraryDialogProps {
  open: boolean;
  workflow: WorkflowRun;
  defaultAgeSegment: ModelLibraryAgeSegment;
  onClose: () => void;
  onGenerateCandidates: () => void;
  generatingCandidates?: boolean;
}

interface UploadFormState {
  title: string;
  age_segment: ModelLibraryItemAgeSegment;
  gender: ModelLibraryGender;
  appearance_direction: string;
  style_tags: string;
}

export function ModelLibraryDialog({
  open,
  workflow,
  defaultAgeSegment,
  onClose,
  onGenerateCandidates,
  generatingCandidates = false,
}: ModelLibraryDialogProps) {
  const [ageSegment, setAgeSegment] = useState<ModelLibraryAgeSegment>(defaultAgeSegment);
  const [source, setSource] = useState<"all" | ModelLibrarySource>("all");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [previewItem, setPreviewItem] = useState<ApparelModelLibraryItem | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadTagsEnabled, setUploadTagsEnabled] = useState(false);
  const [form, setForm] = useState<UploadFormState>({
    title: "",
    age_segment: defaultAgeSegment === "all" ? "user_favorites" : defaultAgeSegment,
    gender: "female",
    appearance_direction: "",
    style_tags: "",
  });
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const libraryQuery = useApparelModelLibraryQuery(
    { age_segment: ageSegment, source, q: query },
    { enabled: open },
  );
  const items = useMemo(() => libraryQuery.data?.items ?? [], [libraryQuery.data?.items]);
  const syncInfo = libraryQuery.data?.sync;
  const selectedItem = useMemo(
    () => items.find((item) => item.id === selectedId) ?? null,
    [items, selectedId],
  );

  const sync = useSyncApparelModelLibraryPresetsMutation({
    onSuccess: (result) => {
      if (result.status === "skipped") {
        toast.info("预设库刚同步过", { description: "已返回最近一次同步结果" });
      } else {
        toast.success("预设库已同步", {
          description: `新增 ${result.added}，更新 ${result.updated}，跳过 ${result.skipped}`,
        });
      }
    },
    onError: (err) =>
      toast.error("同步预设失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const selectItem = useSelectApparelModelLibraryItemMutation(workflow.id, {
    onSuccess: () => {
      toast.success("已选入模特候选");
      onClose();
    },
    onError: (err) =>
      toast.error("选择模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const uploadImage = useUploadImageMutation();
  const createItem = useCreateApparelModelLibraryItemMutation({
    onSuccess: (item) => {
      toast.success("已加入我的模特库");
      setUploadOpen(false);
      setUploadFile(null);
      setSelectedId(item.id);
    },
    onError: (err) =>
      toast.error("登记模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const deleteItem = useDeleteApparelModelLibraryItemMutation({
    onSuccess: () => toast.success("已从当前视图移除"),
    onError: (err) =>
      toast.error("移除失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    const previousActive = document.activeElement as HTMLElement | null;
    document.body.style.overflow = "hidden";
    const raf = requestAnimationFrame(() => dialogRef.current?.focus({ preventScroll: true }));
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      // Tab / Shift+Tab 在 dialog 内循环（焦点陷阱）
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusables = dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (focusables.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (event.shiftKey) {
        if (active === first || !dialog.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
      previousActive?.focus?.();
    };
  }, [onClose, open]);

  const submitUpload = async () => {
    if (!uploadFile) {
      toast.warning("请选择模特图");
      return;
    }
    const title = form.title.trim() || uploadFile.name.replace(/\.[^.]+$/, "");
    const uploaded = await uploadImage.mutateAsync(uploadFile);
    createItem.mutate({
      source: "user_upload",
      image_id: uploaded.id,
      title,
      age_segment: form.age_segment,
      gender: form.gender,
      appearance_direction: form.appearance_direction.trim() || null,
      style_tags: uploadTagsEnabled ? splitTags(form.style_tags) : [],
    });
  };

  const chooseSelected = () => {
    if (!selectedItem) return;
    selectItem.mutate(selectedItem.id);
  };

  return (
    <AnimatePresence>
      {open ? (
        <motion.div
          className="fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/60 p-0 backdrop-blur-md md:items-center md:p-5"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.16 }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) onClose();
          }}
        >
          <motion.div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-label="模特库"
            tabIndex={-1}
            initial={{ opacity: 0, y: 24, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 18, scale: 0.98 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
            className="flex h-[92dvh] w-full max-w-6xl flex-col overflow-hidden rounded-t-xl border border-[var(--border)] bg-[var(--bg-0)] shadow-[var(--shadow-2)] focus:outline-none md:h-[82vh] md:rounded-md"
          >
            <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border)] bg-white/[0.035] px-4 py-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <Library className="h-4 w-4 text-[var(--amber-300)]" />
                  <h2 className="text-base font-semibold text-[var(--fg-0)]">模特库</h2>
                  {syncInfo?.last_success_at ? (
                    <span className="hidden text-xs text-[var(--fg-2)] sm:inline">
                      上次同步 {formatShortDate(syncInfo.last_success_at)}
                    </span>
                  ) : null}
                </div>
                <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                  全站预设、我的收藏和上传模特会在这里合并展示。
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {syncInfo?.can_sync ? (
                  <Button
                    size="sm"
                    variant="outline"
                    loading={sync.isPending}
                    onClick={() => sync.mutate()}
                    leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
                  >
                    同步预设
                  </Button>
                ) : null}
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => setUploadOpen((value) => !value)}
                  leftIcon={<Upload className="h-3.5 w-3.5" />}
                >
                  上传
                </Button>
                <button
                  type="button"
                  onClick={onClose}
                  className="inline-flex h-9 w-9 cursor-pointer items-center justify-center rounded-md border border-[var(--border)] text-[var(--fg-1)] transition-colors hover:bg-white/8 hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/50"
                  aria-label="关闭模特库"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </header>

            <div className="grid min-h-0 flex-1 md:grid-cols-[192px_minmax(0,1fr)]">
              <aside className="hidden border-r border-[var(--border)] bg-white/[0.02] p-3 md:block">
                <p className="mb-2 text-xs font-medium text-[var(--fg-2)]">来源</p>
                <div className="space-y-1">
                  {SOURCE_FILTERS.map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => setSource(value)}
                      className={cn(
                        "flex h-9 w-full cursor-pointer items-center justify-between rounded-md px-3 text-sm transition-colors",
                        source === value
                          ? "bg-[var(--accent-soft)] text-[var(--amber-300)]"
                          : "text-[var(--fg-1)] hover:bg-white/6 hover:text-[var(--fg-0)]",
                      )}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <Button
                  className="mt-4"
                  fullWidth
                  variant="primary"
                  loading={generatingCandidates}
                  onClick={onGenerateCandidates}
                  leftIcon={<WandSparkles className="h-4 w-4" />}
                >
                  生成模特候选
                </Button>
              </aside>

              <main className="flex min-h-0 flex-col">
                <div className="shrink-0 border-b border-[var(--border)] p-3">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
                    <div className="flex min-w-0 flex-1 gap-1 overflow-x-auto pb-1">
                      {AGE_TABS.map(([value, label]) => (
                        <button
                          key={value}
                          type="button"
                          onClick={() => setAgeSegment(value)}
                          className={cn(
                            "h-8 shrink-0 cursor-pointer rounded-md border px-3 text-xs transition-colors",
                            ageSegment === value
                              ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                              : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/6 hover:text-[var(--fg-0)]",
                          )}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    <div className="flex gap-2">
                      <Input
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                        leftIcon={<Search className="h-4 w-4" />}
                        placeholder="搜索名称、标签"
                        wrapperClassName="w-full lg:w-64"
                      />
                      <select
                        value={source}
                        onChange={(event) =>
                          setSource(event.target.value as "all" | ModelLibrarySource)
                        }
                        className="h-9 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none md:hidden"
                      >
                        {SOURCE_FILTERS.map(([value, label]) => (
                          <option key={value} value={value}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>

                  {uploadOpen ? (
                    <div className="mt-3 grid gap-3 rounded-md border border-[var(--border)] bg-white/[0.03] p-3 lg:grid-cols-[minmax(0,1fr)_160px]">
                      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-6">
                        <Input
                          label="名称"
                          value={form.title}
                          onChange={(event) => setForm((prev) => ({ ...prev, title: event.target.value }))}
                          placeholder="我的高级简洁女模特"
                          wrapperClassName="xl:col-span-2"
                        />
                        <label className="flex flex-col gap-1">
                          <span className="text-xs font-medium text-[var(--fg-1)]">年龄段</span>
                          <select
                            value={form.age_segment}
                            onChange={(event) =>
                              setForm((prev) => ({
                                ...prev,
                                age_segment: event.target.value as ModelLibraryItemAgeSegment,
                              }))
                            }
                            className="h-9 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none"
                          >
                            {AGE_TABS.filter(([value]) => value !== "all").map(([value, label]) => (
                              <option key={value} value={value}>
                                {label}
                              </option>
                            ))}
                          </select>
                        </label>
                        <div className="flex flex-col gap-1">
                          <span className="text-xs font-medium text-[var(--fg-1)]">目标文件夹</span>
                          <div className="flex h-9 items-center rounded-md border border-[var(--border)] bg-black/15 px-3 font-mono text-xs text-[var(--fg-1)]">
                            {AGE_FOLDER_BY_SEGMENT[form.age_segment]}/{form.gender}
                          </div>
                        </div>
                        <label className="flex flex-col gap-1">
                          <span className="text-xs font-medium text-[var(--fg-1)]">性别</span>
                          <select
                            value={form.gender}
                            onChange={(event) =>
                              setForm((prev) => ({
                                ...prev,
                                gender: event.target.value as ModelLibraryGender,
                              }))
                            }
                            className="h-9 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none"
                          >
                            {GENDER_OPTIONS.map(([value, label]) => (
                              <option key={value} value={value}>
                                {label}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="flex flex-col gap-1">
                          <span className="text-xs font-medium text-[var(--fg-1)]">标签</span>
                          <button
                            type="button"
                            onClick={() => setUploadTagsEnabled((value) => !value)}
                            className={cn(
                              "h-9 rounded-md border px-3 text-left text-sm transition-colors",
                              uploadTagsEnabled
                                ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                                : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-1)]",
                            )}
                          >
                            {uploadTagsEnabled ? "填写标签" : "不填标签"}
                          </button>
                        </label>
                        {uploadTagsEnabled ? (
                          <Input
                            label="标签内容"
                            value={form.style_tags}
                            onChange={(event) =>
                              setForm((prev) => ({ ...prev, style_tags: event.target.value }))
                            }
                            placeholder="高级简洁、棚拍"
                            wrapperClassName="md:col-span-2 xl:col-span-2"
                          />
                        ) : null}
                      </div>
                      <div className="flex items-end gap-2">
                        <input
                          ref={fileInputRef}
                          type="file"
                          accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"
                          className="hidden"
                          onChange={(event) => setUploadFile(event.target.files?.[0] ?? null)}
                        />
                        <Button
                          className="min-w-0 flex-1"
                          variant="secondary"
                          onClick={() => fileInputRef.current?.click()}
                          leftIcon={<ImagePlus className="h-4 w-4" />}
                        >
                          {uploadFile ? uploadFile.name : "选图"}
                        </Button>
                        <Button
                          variant="primary"
                          loading={uploadImage.isPending || createItem.isPending}
                          onClick={submitUpload}
                        >
                          加入
                        </Button>
                      </div>
                    </div>
                  ) : null}
                </div>

                <div className="min-h-0 flex-1 overflow-y-auto p-3">
                  {libraryQuery.isPending ? (
                    <div className="flex h-64 items-center justify-center gap-2 text-sm text-[var(--fg-2)]">
                      <Spinner size={20} />
                      加载模特库
                    </div>
                  ) : items.length === 0 ? (
                    <div className="flex h-64 flex-col items-center justify-center rounded-md border border-dashed border-[var(--border)] bg-white/[0.02] px-4 text-center">
                      <Library className="h-8 w-8 text-[var(--fg-2)]" />
                      <p className="mt-3 text-sm font-medium text-[var(--fg-0)]">当前筛选没有模特</p>
                      <p className="mt-1 text-xs text-[var(--fg-2)]">
                        上传私有模特，或同步 GitHub 预设文件夹后再查看。
                      </p>
                    </div>
                  ) : (
                    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
                      {items.map((item) => (
                        <ModelLibraryCard
                          key={item.id}
                          item={item}
                          selected={selectedId === item.id}
                          onSelect={() => setSelectedId(item.id)}
                          onPreview={() => setPreviewItem(item)}
                          onDelete={() => deleteItem.mutate(item.id)}
                          deleting={deleteItem.isPending}
                        />
                      ))}
                    </div>
                  )}
                </div>
              </main>
            </div>

            <footer className="flex shrink-0 flex-col gap-2 border-t border-[var(--border)] bg-[var(--bg-0)] px-4 py-3 md:flex-row md:items-center md:justify-between">
              <div className="min-w-0 text-xs text-[var(--fg-2)]">
                {selectedItem ? (
                  <span>
                    已选 {selectedItem.title} · {SOURCE_LABEL[selectedItem.source]} ·{" "}
                    {AGE_LABEL[selectedItem.age_segment]}
                  </span>
                ) : (
                  <span>选择一个库内模特后，会创建 ready 候选并回到现有确认流程。</span>
                )}
              </div>
              <div className="flex gap-2">
                <Button
                  className="md:hidden"
                  variant="secondary"
                  loading={generatingCandidates}
                  onClick={onGenerateCandidates}
                  leftIcon={<WandSparkles className="h-4 w-4" />}
                >
                  生成候选
                </Button>
                <Button
                  variant="primary"
                  loading={selectItem.isPending}
                  disabled={!selectedItem}
                  onClick={chooseSelected}
                  leftIcon={<Check className="h-4 w-4" />}
                >
                  设为当前模特
                </Button>
              </div>
            </footer>
          </motion.div>

          {previewItem ? (
            <div
              className="fixed inset-0 z-[calc(var(--z-dialog)+1)] flex items-center justify-center bg-black/80 p-4"
              onMouseDown={(event) => {
                if (event.target === event.currentTarget) setPreviewItem(null);
              }}
            >
              <div className="relative h-[82vh] w-full max-w-3xl overflow-hidden rounded-md border border-[var(--border)] bg-black">
                <Image
                  src={previewItem.image_url}
                  alt={previewItem.title}
                  fill
                  unoptimized
                  className="object-contain"
                  sizes="90vw"
                />
                <button
                  type="button"
                  onClick={() => setPreviewItem(null)}
                  className="absolute right-3 top-3 inline-flex h-9 w-9 items-center justify-center rounded-md bg-black/70 text-white"
                  aria-label="关闭大图"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>
          ) : null}
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

function ModelLibraryCard({
  item,
  selected,
  deleting,
  onSelect,
  onPreview,
  onDelete,
}: {
  item: ApparelModelLibraryItem;
  selected: boolean;
  deleting: boolean;
  onSelect: () => void;
  onPreview: () => void;
  onDelete: () => void;
}) {
  return (
    <article
      className={cn(
        "group overflow-hidden rounded-md border bg-white/[0.035] transition-colors",
        selected ? "border-[var(--border-amber)]" : "border-[var(--border)] hover:border-[var(--border-strong)]",
      )}
    >
      <button
        type="button"
        onClick={onSelect}
        className="relative block aspect-[4/5] w-full cursor-pointer overflow-hidden bg-[var(--bg-2)]"
      >
        <Image
          src={item.thumb_url || item.image_url}
          alt={item.title}
          fill
          unoptimized
          sizes="(max-width: 768px) 48vw, 220px"
          className="object-cover transition-transform duration-200 group-hover:scale-[1.015]"
        />
        <span className="absolute left-2 top-2 rounded-md bg-black/65 px-2 py-1 text-[10px] text-white backdrop-blur">
          {SOURCE_LABEL[item.source]}
        </span>
        {selected ? (
          <span className="absolute right-2 top-2 inline-flex h-6 w-6 items-center justify-center rounded-full bg-[var(--accent)] text-black">
            <Check className="h-3.5 w-3.5" />
          </span>
        ) : null}
      </button>
      <div className="p-2.5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-[var(--fg-0)]">{item.title}</p>
            <p className="mt-0.5 text-[11px] text-[var(--fg-2)]">
              {AGE_LABEL[item.age_segment]}{item.gender ? ` · ${item.gender}` : ""}
            </p>
          </div>
          <button
            type="button"
            onClick={onDelete}
            disabled={deleting}
            className="inline-flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-md text-[var(--fg-2)] transition-colors hover:bg-white/8 hover:text-[var(--fg-0)] disabled:cursor-not-allowed disabled:opacity-50"
            aria-label={item.source === "preset" ? "隐藏预设" : "删除条目"}
            title={item.source === "preset" ? "隐藏预设" : "删除条目"}
          >
            {deleting ? <Spinner size={12} /> : <MoreHorizontal className="h-4 w-4" />}
          </button>
        </div>
        {item.style_tags.length > 0 ? (
          <div className="mt-2 flex gap-1 overflow-hidden">
            {item.style_tags.slice(0, 2).map((tag) => (
              <span
                key={tag}
                className="truncate rounded-md border border-[var(--border)] px-1.5 py-0.5 text-[10px] text-[var(--fg-2)]"
              >
                {tag}
              </span>
            ))}
          </div>
        ) : null}
        <div className="mt-2 grid grid-cols-2 gap-1.5">
          <Button size="sm" variant="outline" onClick={onPreview} leftIcon={<Eye className="h-3.5 w-3.5" />}>
            大图
          </Button>
          <Button
            size="sm"
            variant="ghost"
            loading={deleting}
            onClick={onDelete}
            leftIcon={<Trash2 className="h-3.5 w-3.5" />}
          >
            {item.source === "preset" ? "隐藏" : "删除"}
          </Button>
        </div>
      </div>
    </article>
  );
}

function splitTags(value: string): string[] {
  return value
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 12);
}
