"use client";

// 共享浏览器：被 ModelLibraryDialog（dialog 模式）和 ModelLibraryPage（page 模式）复用。
// 抽出原 ModelLibraryDialog 里的"浏览/筛选/搜索/上传/grid/卡片"逻辑；
// 不含 dialog 外壳和"生成模特候选"按钮——交给调用方决定。
//
// 交互规则（统一）：
//  - 点击卡片缩略图 = 打开 Lightbox 大图；左右键翻页
//  - 选择模特只能通过 Lightbox 内 action（dialog 模式注入）完成；卡片本身不持有 selected 状态
//
// 关键约束（参考 apps/web/AGENTS.md）：
//  - 禁止 render 阶段访问 ref / 调用 Date.now()
//  - 禁止 effect 中无依赖控制地 setState（这里依赖 mode 切换，没有循环）
//
// onSelectItem prop：dialog 模式下，传给 lightbox action 的 onClick；
// page 模式下传 undefined 即可，无 action 注入。

import { AnimatePresence, motion } from "framer-motion";
import {
  Bookmark,
  ImagePlus,
  Library,
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
import { Input } from "@/components/ui/primitives/Input";
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

const SOURCE_FILTERS: Array<[BrowserSource, string]> = [
  ["all", "全部"],
  ["preset", "全站预设"],
  ["favorite", "我的收藏"],
  ["user_upload", "我的上传"],
  ["generated", "生成入库"],
  ["unsaved_jobs", "待入库"],
];

// 外貌方向 chip：第一个固定 "all=全部"，其余按 MODEL_LIBRARY_APPEARANCE_LABEL 顺序
const APPEARANCE_TABS: Array<[ModelLibraryAppearance, string]> = [
  ["all", "全部"],
  ...(Object.entries(MODEL_LIBRARY_APPEARANCE_LABEL) as Array<
    [Exclude<ModelLibraryAppearance, "all">, string]
  >),
];

const AGE_LABEL = Object.fromEntries(AGE_TABS) as Record<ModelLibraryAgeSegment, string>;

export interface ModelLibraryBrowserProps {
  /**
   * dialog 模式必须传 workflow；page 模式可不传。
   * @deprecated 当前实现并未真正读取 workflow（选择 mutation 由父组件在 onSelectItem 内承接）；
   *   为保留 prop 形态，函数体内仍以 `void workflow` 显式标记不使用。
   */
  workflow?: WorkflowRun;
  /**
   * page  : 独立页中央，没有 dialog 外壳
   * dialog: 嵌入 ModelLibraryDialog 内部，紧凑布局
   */
  mode: "page" | "dialog";
  defaultAgeSegment?: ModelLibraryAgeSegment;
  /**
   * 选模特回调（dialog 模式用）：当用户在 Lightbox 内点「设为当前模特」时被调用。
   * 卡片点击不再触发此回调；卡片只负责打开 Lightbox。
   * 上层（Dialog）负责把这个回调接到现有 useSelectApparelModelLibraryItemMutation。
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
  // workflow 仅 dialog 模式语义上必传；page 模式无 selection 概念
  void workflow;
  const [ageSegment, setAgeSegment] = useState<ModelLibraryAgeSegment>(defaultAgeSegment);
  const [appearance, setAppearance] = useState<ModelLibraryAppearance>("all");
  const [source, setSource] = useState<BrowserSource>("all");
  const [query, setQuery] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [mobileFilterOpen, setMobileFilterOpen] = useState(false);
  // 上传成功后 highlight 一下，纯视觉反馈，不参与"已选"语义
  const [lastUploadedId, setLastUploadedId] = useState<string | null>(null);

  // loser 视图：跳过 list API，改用 jobs API 平铺生成但未入库的图
  const isLoserView = source === "unsaved_jobs";
  const libraryQuery = useApparelModelLibraryQuery(
    {
      age_segment: ageSegment,
      // 后端不认识 unsaved_jobs；loser 模式下传 "all"，反正 enabled=false 不会发请求
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

  // 把 loser items 适配成 ApparelModelLibraryItem-like 形状，复用既有卡片
  const items = useMemo<ApparelModelLibraryItem[]>(() => {
    if (isLoserView) {
      const jobs = jobsQuery.data?.items ?? [];
      const out: ApparelModelLibraryItem[] = [];
      for (const job of jobs) {
        if (job.status !== "succeeded" && job.status !== "partial") continue;
        for (const it of job.items) {
          if (it.saved_item_id != null) continue; // 已入库的不显示
          // 客户端 filter：与全局 chips 一致
          const itemAppearance = (it.appearance_direction || job.appearance_direction || "") as
            | ModelLibraryAppearance
            | "";
          if (appearance !== "all" && itemAppearance !== appearance) continue;
          if (ageSegment !== "all" && (job.age_segment ?? "") !== ageSegment) continue;
          // 简单 q 匹配 style_tags / appearance / gender
          const haystack = [...it.style_tags, itemAppearance, job.gender ?? ""]
            .join(" ")
            .toLowerCase();
          const q = query.trim().toLowerCase();
          if (q && !haystack.includes(q)) continue;
          // id 编入 workflow_run_id + image_id，保存时再 split 出来
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

  // 当前可见 items 转 LightboxItem[]：lightbox 翻页用
  const visibleLightboxItems = useMemo<LightboxItem[]>(
    () =>
      items.map((item) => ({
        id: item.id,
        url: item.image_url,
        thumbUrl: item.thumb_url ?? undefined,
        previewUrl: item.thumb_url ?? undefined,
        prompt: item.title,
      })),
    [items],
  );

  // 仅 dialog 模式：构造给 lightbox 的 action 工厂；卡片点击时同步注入 store
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
    <div className={cn("flex min-h-0 flex-1 flex-col", className)}>
      {showHeader ? (
        <header className="shrink-0 bg-transparent px-4 py-3">
          {/* 第一行：标题 + 上传按钮 */}
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 min-w-0">
              <Library className="h-4 w-4 text-[var(--amber-300)] shrink-0" />
              <h2 className="font-display text-base font-semibold text-[var(--fg-0)] truncate">
                模特库
              </h2>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                size="sm"
                variant="primary"
                onClick={() => setUploadOpen(true)}
                leftIcon={<Upload className="h-3.5 w-3.5" />}
              >
                上传
              </Button>
              {headerExtra}
            </div>
          </div>
          {/* 第二行：同步状态 + 同步按钮（次要 link 样式） */}
          <div className="mt-1 flex items-center gap-3 text-xs text-[var(--fg-2)]">
            {syncInfo?.last_success_at ? (
              <span>上次同步 {formatShortDate(syncInfo.last_success_at)}</span>
            ) : (
              <span>全站预设、收藏、上传与生成入库合并展示</span>
            )}
            {syncInfo?.can_sync ? (
              <button
                type="button"
                onClick={() => sync.mutate()}
                disabled={sync.isPending}
                className="inline-flex items-center gap-1 cursor-pointer text-[var(--amber-300)] hover:text-[var(--amber-200)] disabled:opacity-50"
              >
                {sync.isPending ? (
                  <Spinner size={12} />
                ) : (
                  <RefreshCw className="h-3 w-3" />
                )}
                同步预设
              </button>
            ) : null}
          </div>
        </header>
      ) : null}

      <div
        className={cn(
          "grid min-h-0 flex-1",
          showSourceSidebar ? "md:grid-cols-[192px_minmax(0,1fr)]" : "",
        )}
      >
        {showSourceSidebar ? (
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
          </aside>
        ) : null}

        <main className="flex min-h-0 flex-col">
          {/* 移动端：sticky 1 行（搜索 + 筛选按钮）；桌面端：完整两行 chip */}
          <div className="shrink-0 border-b border-[var(--border)] bg-[var(--bg-0)]/80 backdrop-blur-sm">
            {/* 移动端紧凑筛选条 */}
            <div className="flex items-center gap-2 p-3 md:hidden">
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                leftIcon={<Search className="h-4 w-4" />}
                placeholder="搜索名称、标签"
                wrapperClassName="flex-1 min-w-0"
              />
              <button
                type="button"
                onClick={() => setMobileFilterOpen(true)}
                className={cn(
                  "inline-flex min-h-11 min-w-11 shrink-0 cursor-pointer items-center gap-1.5 rounded-md border px-3 text-xs transition-colors",
                  activeFilterCount > 0
                    ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                    : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/6",
                )}
              >
                <SlidersHorizontal className="h-3.5 w-3.5" />
                筛选
                {activeFilterCount > 0 ? (
                  <span className="font-mono">({activeFilterCount})</span>
                ) : null}
              </button>
            </div>

            {/* 桌面端完整筛选区 */}
            <div className="hidden flex-col gap-2 p-3 md:flex">
              {/* 年龄 chip 行 */}
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
              {/* 外貌 chip 行 */}
              <div className="flex min-w-0 flex-1 gap-1 overflow-x-auto pb-1">
                {APPEARANCE_TABS.map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setAppearance(value)}
                    className={cn(
                      "h-8 shrink-0 cursor-pointer rounded-md border px-3 text-xs transition-colors",
                      appearance === value
                        ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                        : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/6 hover:text-[var(--fg-0)]",
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {/* 搜索 + 来源（无 sidebar 时显示） */}
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
                  className={cn(
                    "h-9 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none",
                    showSourceSidebar ? "md:hidden" : "",
                  )}
                >
                  {SOURCE_FILTERS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            {isLoadingItems ? (
              <div className="flex h-64 items-center justify-center gap-2 text-sm text-[var(--fg-2)]">
                <Spinner size={20} />
                {isLoserView ? "加载待入库图" : "加载模特库"}
              </div>
            ) : items.length === 0 ? (
              <div className="flex h-64 flex-col items-center justify-center rounded-md border border-dashed border-[var(--border)] bg-white/[0.02] px-4 text-center">
                <Library className="h-8 w-8 text-[var(--fg-2)]" />
                <p className="mt-3 text-sm font-medium text-[var(--fg-0)]">
                  当前筛选没有模特
                </p>
                <p className="mt-1 text-xs text-[var(--fg-2)]">
                  上传私有模特、生成新模特，或同步 GitHub 预设文件夹后再查看。
                </p>
              </div>
            ) : (
              <motion.div
                className={cn(
                  "grid gap-2.5 md:gap-3",
                  mode === "page"
                    ? "grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6"
                    : "grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5",
                )}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.18 }}
              >
                {items.map((item) => (
                  <ModelLibraryCard
                    key={item.id}
                    item={item}
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

function ModelLibraryCard({
  item,
  highlighted,
  deleting,
  onOpenLightbox,
  onDelete,
  onSaveLoser,
}: {
  item: ApparelModelLibraryItem;
  /** 上传刚成功的视觉反馈（amber ring），不参与"已选"语义 */
  highlighted: boolean;
  deleting: boolean;
  onOpenLightbox: () => void;
  onDelete: () => void;
  /** 仅 loser 视图传入：未入库图的快速收藏 */
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

  // 命中 appearance label 时显示中文徽标
  const appearanceLabel =
    item.appearance_direction &&
    item.appearance_direction in MODEL_LIBRARY_APPEARANCE_LABEL
      ? MODEL_LIBRARY_APPEARANCE_LABEL[
          item.appearance_direction as Exclude<ModelLibraryAppearance, "all">
        ]
      : null;

  return (
    <article
      className={cn(
        "group overflow-hidden rounded-xl border bg-[var(--bg-2)] transition-all",
        highlighted
          ? "border-[var(--border-amber)] ring-2 ring-[var(--amber-400)] ring-offset-2 ring-offset-[var(--bg-0)]"
          : "border-[var(--border)] hover:border-[var(--border-strong)] hover:shadow-[var(--shadow-2)]",
      )}
    >
      {/* 整块缩略图 = 打开 Lightbox */}
      <button
        type="button"
        onClick={onOpenLightbox}
        aria-label={`查看 ${item.title} 大图`}
        className="relative block aspect-[4/5] w-full cursor-zoom-in overflow-hidden bg-[var(--bg-3)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
      >
        <Image
          src={item.thumb_url || item.image_url}
          alt={item.title}
          fill
          unoptimized
          sizes="(max-width: 768px) 48vw, 220px"
          className="object-cover transition-transform duration-200 group-hover:scale-[1.015]"
        />
        {/* 左上角小胶囊：来源（loser 视图特殊标识为"待入库"，amber 调） */}
        <span
          className={cn(
            "absolute left-2 top-2 rounded-full px-2 py-0.5 text-[10px] backdrop-blur",
            isLoser
              ? "bg-[var(--amber-400)]/85 text-[var(--bg-0)]"
              : "bg-black/65 text-white",
          )}
        >
          {isLoser ? "待入库" : SOURCE_LABEL_SHORT[item.source]}
        </span>
        {/* 右下角浮起：外貌中文徽标 */}
        {appearanceLabel ? (
          <span className="absolute bottom-2 right-2 rounded-full bg-black/65 px-2 py-0.5 text-[10px] text-[var(--amber-200)] backdrop-blur">
            {appearanceLabel}
          </span>
        ) : null}
      </button>
      <div className="p-2.5">
        <p className="truncate text-[13px] font-medium text-[var(--fg-0)] md:text-sm">
          {item.title}
        </p>
        <p className="mt-0.5 text-[10px] text-[var(--fg-2)] md:text-[11px]">
          {AGE_LABEL[item.age_segment]}
          {item.gender ? ` · ${item.gender}` : ""}
        </p>
        {/* 风格标签：完整 wrap */}
        {item.style_tags.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-1">
            {item.style_tags.map((tag) => (
              <span
                key={tag}
                className="rounded-full border border-[var(--border)] px-2 py-0.5 text-[10px] text-[var(--fg-2)]"
              >
                {tag}
              </span>
            ))}
          </div>
        ) : (
          <p className="mt-2 text-[10px] text-[var(--fg-3)]">未识别</p>
        )}
        {/* icon 工具行（删除/识别），不再重复"预览"按钮——卡图本身就是预览入口 */}
        <div className="mt-2.5 flex items-center gap-1.5">
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
              leftIcon={<Bookmark className="h-3.5 w-3.5" />}
              className="flex-1"
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
                className="inline-flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-md text-[var(--fg-2)] transition-colors hover:bg-white/8 hover:text-[var(--amber-300)] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {autoTag.isPending ? (
                  <Spinner size={12} />
                ) : (
                  <Sparkles className="h-4 w-4" />
                )}
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
                  "inline-flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-50",
                  confirmingDelete
                    ? "bg-[var(--danger-soft)] text-[var(--danger)]"
                    : "text-[var(--fg-2)] hover:bg-white/8 hover:text-[var(--danger)]",
                )}
              >
                {deleting ? <Spinner size={12} /> : <Trash2 className="h-4 w-4" />}
              </button>
              {/* 占位让 grid 行高一致；右侧不再有"预览大图"按钮 */}
              <span aria-hidden className="flex-1" />
            </>
          )}
        </div>
      </div>
    </article>
  );
}

// 短版来源标签（卡片左上角徽标用，更紧凑）
const SOURCE_LABEL_SHORT: Record<ModelLibrarySource, string> = {
  preset: "预设",
  favorite: "收藏",
  user_upload: "上传",
  generated: "生成",
};

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
      className="fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/60 backdrop-blur-md md:items-center md:p-5"
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
        className="flex max-h-[92dvh] w-full flex-col overflow-hidden rounded-t-2xl border border-[var(--border)] bg-[var(--bg-0)] shadow-[var(--shadow-3)] md:max-w-2xl md:rounded-xl"
      >
        {/* 头部 */}
        <header className="flex items-center justify-between gap-3 border-b border-[var(--border)] px-4 py-3">
          <h3 className="font-display text-base font-semibold text-[var(--fg-0)]">
            上传到模特库
          </h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-9 w-9 min-h-11 min-w-11 cursor-pointer items-center justify-center rounded-md text-[var(--fg-2)] hover:bg-white/8 hover:text-[var(--fg-0)] md:h-8 md:w-8 md:min-h-8 md:min-w-8"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        {/* body */}
        <div className="grid gap-3 overflow-y-auto p-4 md:grid-cols-2">
          <Input
            label="名称"
            value={form.title}
            onChange={(event) =>
              setForm((prev) => ({ ...prev, title: event.target.value }))
            }
            placeholder="我的高级简洁女模特"
            wrapperClassName="md:col-span-2"
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
              className="h-11 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none md:h-9"
            >
              {AGE_TABS.filter(([value]) => value !== "all").map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
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
              className="h-11 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none md:h-9"
            >
              {GENDER_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <div className="flex flex-col gap-1 md:col-span-2">
            <span className="text-xs font-medium text-[var(--fg-1)]">目标文件夹</span>
            <div className="flex h-9 items-center rounded-md border border-[var(--border)] bg-black/15 px-3 font-mono text-xs text-[var(--fg-1)]">
              {AGE_FOLDER_BY_SEGMENT[form.age_segment]}/{form.gender}
            </div>
          </div>
          {/* 外貌方向 chip 选择器 */}
          <div className="flex flex-col gap-1.5 md:col-span-2">
            <span className="text-xs font-medium text-[var(--fg-1)]">外貌方向（可选）</span>
            <div className="flex flex-wrap gap-1.5">
              <button
                type="button"
                onClick={() =>
                  setForm((prev) => ({ ...prev, appearance_direction: "" }))
                }
                className={cn(
                  "min-h-11 cursor-pointer rounded-md border px-3 text-xs transition-colors md:h-8 md:min-h-0",
                  form.appearance_direction === ""
                    ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                    : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/6",
                )}
              >
                未指定
              </button>
              {(
                Object.entries(MODEL_LIBRARY_APPEARANCE_LABEL) as Array<
                  [Exclude<ModelLibraryAppearance, "all">, string]
                >
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() =>
                    setForm((prev) => ({ ...prev, appearance_direction: value }))
                  }
                  className={cn(
                    "min-h-11 cursor-pointer rounded-md border px-3 text-xs transition-colors md:h-8 md:min-h-0",
                    form.appearance_direction === value
                      ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                      : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/6",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {/* 风格标签 toggle + 内容 */}
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-[var(--fg-1)]">风格标签</span>
            <button
              type="button"
              onClick={() => setUploadTagsEnabled((value) => !value)}
              className={cn(
                "h-11 rounded-md border px-3 text-left text-sm transition-colors md:h-9",
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
            />
          ) : (
            <div className="hidden md:block" />
          )}
          {/* 文件选择 */}
          <div className="flex flex-col gap-1 md:col-span-2">
            <span className="text-xs font-medium text-[var(--fg-1)]">模特图</span>
            <input
              ref={fileInputRef}
              type="file"
              accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"
              className="hidden"
              onChange={(event) => setUploadFile(event.target.files?.[0] ?? null)}
            />
            <Button
              variant="secondary"
              onClick={() => fileInputRef.current?.click()}
              leftIcon={<ImagePlus className="h-4 w-4" />}
              fullWidth
            >
              {uploadFile ? uploadFile.name : "选图"}
            </Button>
          </div>
        </div>

        {/* footer */}
        <footer className="flex items-center justify-end gap-2 border-t border-[var(--border)] px-4 py-3 pb-[calc(0.75rem+env(safe-area-inset-bottom,0px))] md:pb-3">
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
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
      className="fixed inset-0 z-[var(--z-dialog)] flex items-end bg-black/60 backdrop-blur-sm md:hidden"
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
        className="flex max-h-[85dvh] w-full flex-col overflow-hidden rounded-t-2xl border-t border-[var(--border)] bg-[var(--bg-0)] shadow-[var(--shadow-3)]"
      >
        <header className="flex items-center justify-between gap-2 border-b border-[var(--border)] px-4 py-3">
          <h3 className="font-display text-sm font-semibold text-[var(--fg-0)]">筛选</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-10 w-10 min-h-11 min-w-11 cursor-pointer items-center justify-center rounded-md text-[var(--fg-2)] hover:bg-white/8"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="flex flex-col gap-4 overflow-y-auto p-4">
          {/* 年龄 */}
          <div>
            <p className="mb-2 text-xs font-medium text-[var(--fg-2)]">年龄段</p>
            <div className="flex flex-wrap gap-1.5">
              {AGE_TABS.map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => onAgeChange(value)}
                  className={cn(
                    "min-h-11 cursor-pointer rounded-md border px-3 text-xs transition-colors",
                    ageSegment === value
                      ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                      : "border-[var(--border)] text-[var(--fg-1)]",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {/* 外貌 */}
          <div>
            <p className="mb-2 text-xs font-medium text-[var(--fg-2)]">外貌方向</p>
            <div className="flex flex-wrap gap-1.5">
              {APPEARANCE_TABS.map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => onAppearanceChange(value)}
                  className={cn(
                    "min-h-11 cursor-pointer rounded-md border px-3 text-xs transition-colors",
                    appearance === value
                      ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                      : "border-[var(--border)] text-[var(--fg-1)]",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {/* 来源 */}
          <div>
            <p className="mb-2 text-xs font-medium text-[var(--fg-2)]">来源</p>
            <div className="flex flex-wrap gap-1.5">
              {SOURCE_FILTERS.map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => onSourceChange(value)}
                  className={cn(
                    "min-h-11 cursor-pointer rounded-md border px-3 text-xs transition-colors",
                    source === value
                      ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                      : "border-[var(--border)] text-[var(--fg-1)]",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        </div>
        <footer className="flex items-center justify-between gap-2 border-t border-[var(--border)] px-4 py-3 pb-[calc(0.75rem+env(safe-area-inset-bottom,0px))]">
          <Button
            variant="ghost"
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
