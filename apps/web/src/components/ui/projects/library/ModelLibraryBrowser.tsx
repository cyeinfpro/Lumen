"use client";

// Editorial 重构：杂志大标题 + portrait 模特卡 + hairline toolbar + underline-on-active chip。
// 共享浏览器：被 ModelLibraryDialog（dialog 模式）和 ModelLibraryPage（page 模式）复用。
//
// 交互规则（统一）：
//  - 点击卡片缩略图 = 打开 Lightbox 大图；左右键翻页
//  - 选择模特只能通过 Lightbox 内 action（dialog 模式注入）完成；卡片本身不持有 selected 状态
//
// 关键约束（参考 apps/web/AGENTS.md）：
//  - 禁止 render 阶段访问 ref / 调用 Date.now()
//  - 禁止 effect 中无依赖控制地 setState

import { AnimatePresence, motion } from "framer-motion";
import {
  Bookmark,
  ImagePlus,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import Image from "next/image";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { toast } from "@/components/ui/primitives/Toast";
import { cn } from "@/lib/utils";
import type { LightboxItem } from "@/components/ui/lightbox/types";
import type {
  ApparelModelLibraryItem,
  ApparelModelLibrarySaveJobItemIn,
  ModelLibraryAgeSegment,
  ModelLibraryAppearance,
  ModelLibraryItemAgeSegment,
  ModelLibrarySource,
  WorkflowRun,
} from "@/lib/apiClient";
import { MODEL_LIBRARY_APPEARANCE_LABEL } from "@/lib/apiClient";
import {
  useApparelModelLibraryJobsQuery,
  useApparelModelLibraryQuery,
  useAutoTagApparelModelLibraryItemMutation,
  useCreateApparelModelLibraryItemMutation,
  useDeleteApparelModelLibraryItemMutation,
  useSaveApparelModelLibraryJobItemMutation,
  useSyncApparelModelLibraryPresetsMutation,
  useUploadImageMutation,
} from "@/lib/queries";
import { useUiStore, type LightboxAction } from "@/store/useUiStore";
import { formatShortDate } from "../utils";

// 浏览器内部 source 联合：在标准 source 之外加 unsaved_jobs（前端伪 source）
type BrowserSource = "all" | ModelLibrarySource | "unsaved_jobs";

const AGE_TABS: Array<[ModelLibraryAgeSegment, string]> = [
  ["all", "全部"],
  ["user_favorites", "收藏"],
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

const SOURCE_FILTERS: Array<[BrowserSource, string]> = [
  ["all", "全部"],
  ["preset", "预设"],
  ["favorite", "收藏"],
  ["user_upload", "上传"],
  ["generated", "生成"],
  ["unsaved_jobs", "待入库"],
];

// 外貌方向 chip：第一个固定 "all=全部"
const APPEARANCE_TABS: Array<[ModelLibraryAppearance, string]> = [
  ["all", "全部"],
  ...(Object.entries(MODEL_LIBRARY_APPEARANCE_LABEL) as Array<
    [Exclude<ModelLibraryAppearance, "all">, string]
  >),
];

const AGE_LABEL = Object.fromEntries(AGE_TABS) as Record<ModelLibraryAgeSegment, string>;

// 短版来源标签（卡片左下角徽标）
const SOURCE_LABEL_SHORT: Record<ModelLibrarySource, string> = {
  preset: "预设",
  favorite: "收藏",
  user_upload: "上传",
  generated: "生成",
};

export interface ModelLibraryBrowserProps {
  /**
   * dialog 模式必须传 workflow；page 模式可不传。
   * @deprecated 当前实现并未真正读取 workflow（选择 mutation 由父组件在 onSelectItem 内承接）
   */
  workflow?: WorkflowRun;
  /**
   * page  : 独立页中央，没有 dialog 外壳
   * dialog: 嵌入 ModelLibraryDialog 内部，紧凑布局
   */
  mode: "page" | "dialog";
  defaultAgeSegment?: ModelLibraryAgeSegment;
  /**
   * 选模特回调（dialog 模式用）
   */
  onSelectItem?: (item: ApparelModelLibraryItem) => void;
  /** dialog 模式下，由父组件控制 lightbox action 的 pending 文案 */
  selectActionLabel?: string;
  /** 是否显示左侧 sourceFilter 列；dialog 模式可能想隐藏 */
  showSourceSidebar?: boolean;
  /** 父级想显示头部信息（同步状态、上传按钮）；page 模式渲染 */
  showHeader?: boolean;
  /** 顶部右上角额外 slot（给 page 模式塞"返回项目"用） */
  headerExtra?: React.ReactNode;
  className?: string;
}

interface UploadFormState {
  title: string;
  age_segment: ModelLibraryItemAgeSegment;
  gender: ModelLibraryGender;
  appearance_direction: Exclude<ModelLibraryAppearance, "all"> | "";
  style_tags: string;
}

export function ModelLibraryBrowser({
  workflow,
  mode,
  defaultAgeSegment = "all",
  onSelectItem,
  selectActionLabel = "设为当前模特",
  showSourceSidebar = true,
  showHeader = true,
  headerExtra,
  className,
}: ModelLibraryBrowserProps) {
  void workflow;
  const [ageSegment, setAgeSegment] = useState<ModelLibraryAgeSegment>(defaultAgeSegment);
  const [appearance, setAppearance] = useState<ModelLibraryAppearance>("all");
  const [source, setSource] = useState<BrowserSource>("all");
  const [query, setQuery] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [mobileFilterOpen, setMobileFilterOpen] = useState(false);
  const [lastUploadedId, setLastUploadedId] = useState<string | null>(null);

  // 待入库视图：跳过 list API，改用 jobs API 平铺生成但未入库的图
  const isLoserView = source === "unsaved_jobs";
  const libraryQuery = useApparelModelLibraryQuery(
    {
      age_segment: ageSegment,
      source: isLoserView ? "all" : source,
      appearance,
      q: query,
    },
    { enabled: !isLoserView },
  );
  const jobsQuery = useApparelModelLibraryJobsQuery({
    enabled: isLoserView,
  });
  const syncInfo = libraryQuery.data?.sync;
  const isLoadingItems = isLoserView ? jobsQuery.isPending : libraryQuery.isPending;

  // 把待入库 items/candidates 适配成 ApparelModelLibraryItem-like 形状
  const items = useMemo<ApparelModelLibraryItem[]>(() => {
    if (isLoserView) {
      const jobs = jobsQuery.data?.items ?? [];
      const out: ApparelModelLibraryItem[] = [];
      for (const job of jobs) {
        if (job.status !== "succeeded" && job.status !== "partial") continue;
        for (const it of [...job.items, ...job.candidates]) {
          if (it.saved_item_id != null) continue;
          const itemAppearance = (it.appearance_direction || job.appearance_direction || "") as
            | ModelLibraryAppearance
            | "";
          if (appearance !== "all" && itemAppearance !== appearance) continue;
          if (ageSegment !== "all" && (job.age_segment ?? "") !== ageSegment) continue;
          const haystack = [...it.style_tags, itemAppearance, job.gender ?? ""]
            .join(" ")
            .toLowerCase();
          const q = query.trim().toLowerCase();
          if (q && !haystack.includes(q)) continue;
          out.push({
            id: `loser:${job.workflow_run_id}:${it.image_id}`,
            source: "generated" as ModelLibrarySource,
            visibility_scope: "user_private",
            title: `${job.gender || "未知"} · ${
              job.age_segment ? AGE_LABEL[job.age_segment] ?? job.age_segment : "—"
            }`,
            age_segment: (job.age_segment ?? "young_adult") as ModelLibraryItemAgeSegment,
            gender: job.gender,
            appearance_direction: itemAppearance || null,
            style_tags: it.style_tags,
            image_url: it.image_url,
            display_url: it.display_url,
            thumb_url: it.thumb_url,
            image_id: it.image_id,
            created_at: job.created_at,
          });
        }
      }
      return out;
    }
    return libraryQuery.data?.items ?? [];
  }, [
    isLoserView,
    jobsQuery.data?.items,
    libraryQuery.data?.items,
    appearance,
    ageSegment,
    query,
  ]);

  const visibleLightboxItems = useMemo<LightboxItem[]>(
    () =>
      items.map((item) => ({
        id: item.id,
        url: item.image_url,
        thumbUrl: item.thumb_url ?? undefined,
        previewUrl: item.display_url ?? item.image_url,
        prompt: item.title,
      })),
    [items],
  );

  const buildLightboxAction = useMemo<
    null | (() => LightboxAction)
  >(() => {
    if (mode !== "dialog" || !onSelectItem) return null;
    const itemMap = new Map<string, ApparelModelLibraryItem>(
      items.map((it) => [it.id, it]),
    );
    return () => ({
      label: selectActionLabel,
      pending: false,
      onClick: (lightboxItem) => {
        const libraryItem = itemMap.get(lightboxItem.id);
        if (libraryItem) onSelectItem(libraryItem);
      },
    });
  }, [items, mode, onSelectItem, selectActionLabel]);

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
  const deleteItem = useDeleteApparelModelLibraryItemMutation({
    onSuccess: () => toast.success("已从当前视图移除"),
    onError: (err) =>
      toast.error("移除失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  // 移动端筛选数：年龄、外貌、来源；非 "all" 计 1
  const activeFilterCount = useMemo(() => {
    let n = 0;
    if (ageSegment !== "all") n += 1;
    if (appearance !== "all") n += 1;
    if (source !== "all") n += 1;
    return n;
  }, [ageSegment, appearance, source]);

  return (
    <div className={cn("flex min-h-0 flex-1 flex-col gap-5", className)}>
      {showHeader ? (
        <header className="border-y border-[var(--border)] py-5 md:py-6">
          <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
            <div className="min-w-0 flex-1">
              <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                Models · Library
              </p>
              <h2 className="mt-2 font-display text-[28px] italic leading-[1] text-[var(--fg-0)] md:text-[36px]">
                浏览模特
              </h2>
              <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
                {syncInfo?.last_success_at ? (
                  <>Last sync · {formatShortDate(syncInfo.last_success_at)}</>
                ) : (
                  <>Preset · favorite · upload · generated</>
                )}
              </p>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              {syncInfo?.can_sync ? (
                <button
                  type="button"
                  onClick={() => sync.mutate()}
                  disabled={sync.isPending}
                  className="inline-flex h-10 items-center gap-2 border border-[var(--border)] px-3 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] disabled:opacity-50"
                >
                  {sync.isPending ? (
                    <Spinner size={12} />
                  ) : (
                    <RefreshCw className="h-3 w-3" />
                  )}
                  Sync
                </button>
              ) : null}
              <Button
                size="md"
                variant="primary"
                onClick={() => setUploadOpen(true)}
                leftIcon={<Upload className="h-3.5 w-3.5" />}
              >
                上传
              </Button>
              {headerExtra}
            </div>
          </div>
        </header>
      ) : null}

      <div
        className={cn(
          "grid min-h-0 flex-1 gap-6",
          showSourceSidebar ? "md:grid-cols-[160px_minmax(0,1fr)]" : "",
        )}
      >
        {showSourceSidebar ? (
          <aside className="hidden md:block">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Source
            </p>
            <div className="mt-3 grid">
              {SOURCE_FILTERS.map(([value, label]) => {
                const active = source === value;
                return (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setSource(value)}
                    className={cn(
                      "group relative flex h-10 cursor-pointer items-center justify-between border-b border-[var(--border)] py-2 font-mono text-[11px] uppercase tracking-[0.16em] transition-colors",
                      active
                        ? "text-[var(--fg-0)]"
                        : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
                    )}
                  >
                    <span>{label}</span>
                    {active ? (
                      <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-[var(--amber-400)]" />
                    ) : null}
                  </button>
                );
              })}
            </div>
          </aside>
        ) : null}

        <main className="flex min-h-0 flex-col gap-5">
          {/* 移动端：紧凑筛选条 */}
          <div className="flex items-center gap-2 md:hidden">
            <div className="relative flex-1 min-w-0">
              <Search className="pointer-events-none absolute left-0 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索名称、标签"
                className="h-11 w-full border-b border-[var(--border)] bg-transparent pl-7 pr-2 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
              />
            </div>
            <button
              type="button"
              onClick={() => setMobileFilterOpen(true)}
              className={cn(
                "inline-flex min-h-11 shrink-0 cursor-pointer items-center gap-1.5 border px-3 font-mono text-[10px] uppercase tracking-[0.16em] transition-colors",
                activeFilterCount > 0
                  ? "border-[var(--border-amber)] text-[var(--amber-300)]"
                  : "border-[var(--border)] text-[var(--fg-1)] hover:border-[var(--border-strong)]",
              )}
            >
              <SlidersHorizontal className="h-3.5 w-3.5" />
              筛选
              {activeFilterCount > 0 ? (
                <span className="tabular-nums">·{activeFilterCount}</span>
              ) : null}
            </button>
          </div>

          {/* 桌面端：完整筛选区 */}
          <div className="hidden md:grid md:gap-4">
            {/* 年龄 chip 行 */}
            <ChipRowGroup label="Age">
              {AGE_TABS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={ageSegment === value}
                  onClick={() => setAgeSegment(value)}
                >
                  {label}
                </Chip>
              ))}
            </ChipRowGroup>
            {/* 外貌 chip 行 */}
            <ChipRowGroup label="Appearance">
              {APPEARANCE_TABS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={appearance === value}
                  onClick={() => setAppearance(value)}
                >
                  {label}
                </Chip>
              ))}
            </ChipRowGroup>
            {/* 搜索 + 来源（无 sidebar 时显示 select） */}
            <div className="flex items-center gap-4">
              <div className="relative w-full max-w-sm">
                <Search className="pointer-events-none absolute left-0 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]" />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜索名称、标签"
                  className="h-10 w-full border-b border-[var(--border)] bg-transparent pl-7 pr-9 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
                />
                {query ? (
                  <button
                    type="button"
                    onClick={() => setQuery("")}
                    aria-label="清除搜索"
                    className="absolute right-0 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 cursor-pointer items-center justify-center text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)]"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                ) : null}
              </div>
              {!showSourceSidebar ? (
                <select
                  value={source}
                  onChange={(event) => setSource(event.target.value as "all" | ModelLibrarySource)}
                  className="h-10 border-b border-[var(--border)] bg-transparent px-1 font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--fg-1)] outline-none focus:border-[var(--amber-400)]"
                >
                  {SOURCE_FILTERS.map(([value, label]) => (
                    <option key={value} value={value} className="bg-[var(--bg-0)]">
                      {label}
                    </option>
                  ))}
                </select>
              ) : null}
            </div>
          </div>

          <div className="min-h-0 flex-1">
            {isLoadingItems ? (
              <div className="flex h-64 items-center justify-center gap-2 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
                <Spinner size={20} />
                {isLoserView ? "Loading queue" : "Loading"}
              </div>
            ) : items.length === 0 ? (
              <EmptyBrowser />
            ) : (
              <motion.div
                className={cn(
                  "grid gap-x-4 gap-y-8 md:gap-x-5 md:gap-y-10",
                  mode === "page"
                    ? "grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6"
                    : "grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5",
                )}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.18 }}
              >
                {items.map((item, index) => (
                  <ModelLibraryCard
                    key={item.id}
                    item={item}
                    order={index}
                    highlighted={lastUploadedId === item.id}
                    onOpenLightbox={() => {
                      const action = buildLightboxAction?.() ?? null;
                      useUiStore
                        .getState()
                        .openLightboxFromItems(visibleLightboxItems, item.id, action);
                    }}
                    onDelete={() => deleteItem.mutate(item.id)}
                    deleting={deleteItem.isPending}
                    onSaveLoser={isLoserView ? item : undefined}
                  />
                ))}
              </motion.div>
            )}
          </div>
        </main>
      </div>

      <AnimatePresence>
        {uploadOpen ? (
          <UploadDialog
            key="upload-dialog"
            defaultAgeSegment={defaultAgeSegment}
            onClose={() => setUploadOpen(false)}
            onCreated={(id) => setLastUploadedId(id)}
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
            onAgeChange={setAgeSegment}
            onAppearanceChange={setAppearance}
            onSourceChange={setSource}
            onClose={() => setMobileFilterOpen(false)}
          />
        ) : null}
      </AnimatePresence>
    </div>
  );
}

function ChipRowGroup({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid gap-2">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        {label}
      </p>
      <div className="-mx-1 flex min-w-0 flex-1 flex-wrap gap-x-4 gap-y-1 overflow-x-auto px-1 pb-1">
        {children}
      </div>
    </div>
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
        "group relative inline-flex min-h-9 shrink-0 cursor-pointer items-center px-1 py-1.5 font-mono text-[11px] uppercase tracking-[0.16em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
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

function EmptyBrowser() {
  return (
    <div className="border-y border-[var(--border)] py-16 md:py-20">
      <div className="grid gap-3">
        <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--amber-300)]">
          Empty
        </p>
        <h4 className="font-display text-[28px] italic leading-[1.05] text-[var(--fg-0)] md:text-[36px]">
          当前筛选没有模特
        </h4>
        <p className="max-w-xl text-[13px] leading-[1.7] text-[var(--fg-1)]">
          上传私有模特、生成新模特，或同步预设文件夹后再查看。
        </p>
      </div>
    </div>
  );
}

// Portrait 模特卡：3/4 大图 + 底部 mono 元数据 + hover micro scale
function ModelLibraryCard({
  item,
  order,
  highlighted,
  deleting,
  onOpenLightbox,
  onDelete,
  onSaveLoser,
}: {
  item: ApparelModelLibraryItem;
  order: number;
  highlighted: boolean;
  deleting: boolean;
  onOpenLightbox: () => void;
  onDelete: () => void;
  onSaveLoser?: ApparelModelLibraryItem;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const isPreset = item.source === "preset";
  const isLoser = onSaveLoser != null;
  const loserParts = isLoser ? item.id.split(":") : [];
  const loserWorkflowRunId =
    loserParts.length >= 3 && loserParts[0] === "loser" ? loserParts[1] : "";
  const loserImageId =
    loserParts.length >= 3 && loserParts[0] === "loser"
      ? loserParts.slice(2).join(":")
      : "";
  const requestDelete = () => {
    if (confirmingDelete) {
      onDelete();
      setConfirmingDelete(false);
      return;
    }
    setConfirmingDelete(true);
    window.setTimeout(() => setConfirmingDelete(false), 3000);
  };
  const autoTag = useAutoTagApparelModelLibraryItemMutation(item.id, {
    onSuccess: (data) =>
      toast.success("已识别风格", {
        description:
          data.style_tags.length > 0 ? data.style_tags.join("、") : "未识别到明显风格",
      }),
    onError: (err) =>
      toast.error("识别失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const saveLoser = useSaveApparelModelLibraryJobItemMutation(
    loserWorkflowRunId,
    loserImageId,
    {
      onSuccess: () => toast.success("已收藏入库"),
      onError: (err) =>
        toast.error("入库失败", {
          description: err instanceof Error ? err.message : "请稍后重试",
        }),
    },
  );

  const appearanceLabel =
    item.appearance_direction &&
    item.appearance_direction in MODEL_LIBRARY_APPEARANCE_LABEL
      ? MODEL_LIBRARY_APPEARANCE_LABEL[
          item.appearance_direction as Exclude<ModelLibraryAppearance, "all">
        ]
      : null;

  return (
    <article className="group relative">
      {/* 缩略图区：portrait 大图 */}
      <button
        type="button"
        onClick={onOpenLightbox}
        aria-label={`查看 ${item.title} 大图`}
        className={cn(
          "relative block aspect-[3/4] w-full cursor-zoom-in overflow-hidden bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          highlighted ? "outline outline-2 outline-offset-2 outline-[var(--amber-400)]" : "",
        )}
      >
        <Image
          src={item.thumb_url || item.image_url}
          alt={item.title}
          fill
          unoptimized
          sizes="(max-width: 640px) 50vw, (max-width: 1024px) 33vw, 240px"
          className="object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.04]"
        />
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-transparent opacity-0 transition-opacity duration-[var(--dur-base)] group-hover:opacity-100"
        />
        {/* N°NN 序号 */}
        <span className="absolute left-2 top-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/85 mix-blend-difference">
          N°{String(order + 1).padStart(2, "0")}
        </span>
        {/* 来源标识 */}
        <span
          className={cn(
            "absolute right-2 top-2 inline-flex items-center font-mono text-[10px] uppercase tracking-[0.18em]",
            isLoser ? "text-[var(--amber-300)]" : "text-white/85 mix-blend-difference",
          )}
        >
          {isLoser ? "待入库" : SOURCE_LABEL_SHORT[item.source]}
        </span>
        {/* 外貌徽标：底部 mono caption */}
        {appearanceLabel ? (
          <span className="absolute bottom-2 right-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/85 mix-blend-difference">
            {appearanceLabel}
          </span>
        ) : null}
      </button>

      {/* 信息区：底部 mono 元数据 */}
      <div className="mt-2.5 grid gap-1">
        <p className="line-clamp-1 text-[14px] font-medium leading-[1.35] text-[var(--fg-0)] transition-colors duration-[var(--dur-base)] group-hover:text-[var(--amber-300)]">
          {item.title}
        </p>
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          <span>{AGE_LABEL[item.age_segment]}</span>
          {item.gender ? (
            <>
              <span aria-hidden className="mx-1.5 text-[var(--fg-3)]">·</span>
              <span>{item.gender === "male" ? "M" : "F"}</span>
            </>
          ) : null}
        </p>
        {item.style_tags.length > 0 ? (
          <p className="line-clamp-1 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
            {item.style_tags.slice(0, 3).join(" · ")}
          </p>
        ) : (
          <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-3)]">
            Untagged
          </p>
        )}

        {/* 操作行 */}
        <div className="mt-1.5 flex items-center gap-3">
          {isLoser ? (
            <Button
              size="sm"
              variant="primary"
              loading={saveLoser.isPending}
              onClick={() => {
                if (!loserWorkflowRunId || !loserImageId) return;
                const body: ApparelModelLibrarySaveJobItemIn = {
                  title: item.title,
                  age_segment: item.age_segment,
                  gender: item.gender === "male" ? "male" : "female",
                  appearance_direction: item.appearance_direction,
                  style_tags: item.style_tags,
                  auto_tag: true,
                };
                saveLoser.mutate(body);
              }}
              leftIcon={<Bookmark className="h-3 w-3" />}
            >
              收藏入库
            </Button>
          ) : (
            <>
              <button
                type="button"
                onClick={() => autoTag.mutate()}
                disabled={autoTag.isPending}
                title="重新识别风格标签"
                aria-label="重新识别风格标签"
                className="inline-flex h-8 cursor-pointer items-center gap-1 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)] transition-colors hover:text-[var(--amber-300)] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {autoTag.isPending ? (
                  <Spinner size={12} />
                ) : (
                  <Sparkles className="h-3 w-3" />
                )}
                Tag
              </button>
              <button
                type="button"
                onClick={requestDelete}
                disabled={deleting}
                title={
                  confirmingDelete
                    ? "再次点击确认删除"
                    : isPreset
                      ? "隐藏预设"
                      : "删除条目"
                }
                aria-label={
                  confirmingDelete
                    ? "再次点击确认删除"
                    : isPreset
                      ? "隐藏预设"
                      : "删除条目"
                }
                className={cn(
                  "inline-flex h-8 cursor-pointer items-center gap-1 font-mono text-[10px] uppercase tracking-[0.16em] transition-colors disabled:cursor-not-allowed disabled:opacity-50",
                  confirmingDelete
                    ? "text-[var(--danger)]"
                    : "text-[var(--fg-2)] hover:text-[var(--danger)]",
                )}
              >
                {deleting ? <Spinner size={12} /> : <Trash2 className="h-3 w-3" />}
                {confirmingDelete ? "Confirm" : isPreset ? "Hide" : "Del"}
              </button>
            </>
          )}
        </div>
      </div>
    </article>
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
    age_segment: defaultAgeSegment === "all" ? "user_favorites" : defaultAgeSegment,
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

  const submit = async () => {
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
      appearance_direction: form.appearance_direction || null,
      style_tags: uploadTagsEnabled ? splitTags(form.style_tags) : [],
    });
  };

  const submitting = uploadImage.isPending || createItem.isPending;

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
        aria-label="上传到模特库"
        initial={{ opacity: 0, y: 24, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 16, scale: 0.98 }}
        transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
        className="mobile-dialog-panel flex w-full flex-col overflow-hidden border border-[var(--border)] bg-[var(--bg-0)] md:max-h-[92dvh] md:max-w-2xl"
      >
        <header className="flex items-start justify-between gap-3 border-b border-[var(--border)] px-5 pb-4 pt-5">
          <div>
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Upload to library
            </p>
            <h3 className="mt-2 font-display text-[24px] italic leading-none text-[var(--fg-0)] md:text-[28px]">
              上传到模特库
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

        <div className="mobile-dialog-scroll grid min-h-0 flex-1 gap-5 overflow-y-auto px-5 py-5 md:grid-cols-2">
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
              {AGE_TABS.filter(([value]) => value !== "all").map(([value, label]) => (
                <option key={value} value={value} className="bg-[var(--bg-0)]">
                  {label}
                </option>
              ))}
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
          <UnderlineLabeled label="外貌方向（可选）" wrapperClass="md:col-span-2">
            <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1">
              <Chip
                active={form.appearance_direction === ""}
                onClick={() =>
                  setForm((prev) => ({ ...prev, appearance_direction: "" }))
                }
              >
                未指定
              </Chip>
              {(
                Object.entries(MODEL_LIBRARY_APPEARANCE_LABEL) as Array<
                  [Exclude<ModelLibraryAppearance, "all">, string]
                >
              ).map(([value, label]) => (
                <Chip
                  key={value}
                  active={form.appearance_direction === value}
                  onClick={() =>
                    setForm((prev) => ({ ...prev, appearance_direction: value }))
                  }
                >
                  {label}
                </Chip>
              ))}
            </div>
          </UnderlineLabeled>
          <UnderlineLabeled label="风格标签">
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
                {uploadTagsEnabled ? "Fill tags" : "Skip tags"}
              </span>
            </button>
          </UnderlineLabeled>
          {uploadTagsEnabled ? (
            <UnderlineLabeled label="标签内容">
              <input
                value={form.style_tags}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, style_tags: event.target.value }))
                }
                placeholder="高级简洁、棚拍"
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
              onChange={(event) => setUploadFile(event.target.files?.[0] ?? null)}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="mt-1.5 flex w-full items-center gap-3 border-b border-[var(--border)] py-3 text-left transition-colors hover:border-[var(--border-strong)]"
            >
              <ImagePlus className="h-4 w-4 text-[var(--fg-2)]" />
              <span className="truncate text-[14px] text-[var(--fg-0)]">
                {uploadFile ? uploadFile.name : "选图"}
              </span>
            </button>
          </div>
        </div>

        <footer className="mobile-dialog-footer flex shrink-0 items-center justify-end gap-2 border-t border-[var(--border)] px-5 py-4">
          <Button variant="outline" onClick={onClose} disabled={submitting}>
            取消
          </Button>
          <Button variant="primary" loading={submitting} onClick={submit}>
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
  children: React.ReactNode;
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

  return (
    <div
      className="fixed inset-0 z-[var(--z-dialog)] flex items-end bg-black/60 backdrop-blur-sm mobile-dialog-shell md:hidden"
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
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Filter
            </p>
            <h3 className="mt-2 font-display text-[22px] italic leading-none text-[var(--fg-0)]">
              筛选
            </h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-10 w-10 cursor-pointer items-center justify-center text-[var(--fg-2)] hover:text-[var(--fg-0)]"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="mobile-dialog-scroll flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-5 py-5">
          {/* 年龄 */}
          <div className="grid gap-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Age
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
          {/* 外貌 */}
          <div className="grid gap-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Appearance
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
          {/* 来源 */}
          <div className="grid gap-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Source
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
        <footer className="mobile-dialog-footer flex shrink-0 items-center justify-between gap-2 border-t border-[var(--border)] px-5 py-4">
          <Button
            variant="outline"
            onClick={() => {
              onAgeChange("all");
              onAppearanceChange("all");
              onSourceChange("all");
            }}
          >
            清空
          </Button>
          <Button variant="primary" onClick={onClose}>
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
