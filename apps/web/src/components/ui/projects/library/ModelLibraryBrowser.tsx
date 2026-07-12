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

import { useMemo, useState, type ReactNode } from "react";

import type { LightboxItem } from "@/components/ui/lightbox/types";
import { toast } from "@/components/ui/primitives/Toast";
import type {
  ApparelModelLibraryJob,
  ApparelModelLibraryJobItem,
  ApparelModelLibraryItem,
  ModelLibraryAgeSegment,
  ModelLibraryAppearance,
  WorkflowRun,
} from "@/lib/apiClient";
import {
  useApparelModelLibraryJobsQuery,
  useApparelModelLibraryQuery,
  useDeleteApparelModelLibraryItemMutation,
  useDeleteApparelModelLibraryItemsMutation,
  useSyncApparelModelLibraryPresetsMutation,
} from "@/lib/queries";
import { cn } from "@/lib/utils";
import { useUiStore, type LightboxAction } from "@/store/useUiStore";

import { formatShortDate } from "../utils";
import { ModelLibraryBrowserOverlays } from "./ModelLibraryBrowserDialogs";
import { ModelLibraryBrowserLayout } from "./ModelLibraryBrowserView";
import {
  AGE_LABEL,
  genderLabel,
  isModelLibraryGender,
  type BrowserSource,
  type ModelLibraryGender,
} from "./modelLibraryBrowserOptions";

interface UnsavedItemFilters {
  ageSegment: ModelLibraryAgeSegment;
  appearance: ModelLibraryAppearance;
  query: string;
}

function jobItemGender(
  item: ApparelModelLibraryJobItem,
  job: ApparelModelLibraryJob,
): ModelLibraryGender | null {
  if (isModelLibraryGender(item.gender)) return item.gender;
  if (isModelLibraryGender(job.gender)) return job.gender;
  return null;
}

function unsavedLibraryItem(
  job: ApparelModelLibraryJob,
  item: ApparelModelLibraryJobItem,
  filters: UnsavedItemFilters,
): ApparelModelLibraryItem | null {
  if (item.saved_item_id != null) return null;
  const appearance = (item.appearance_direction ||
    job.appearance_direction ||
    "") as ModelLibraryAppearance | "";
  const gender = jobItemGender(item, job);
  if (filters.appearance !== "all" && appearance !== filters.appearance) {
    return null;
  }
  if (
    filters.ageSegment !== "all" &&
    (job.age_segment ?? "") !== filters.ageSegment
  ) {
    return null;
  }
  const query = filters.query.trim().toLowerCase();
  const haystack = [...item.style_tags, appearance, gender ?? ""]
    .join(" ")
    .toLowerCase();
  if (query && !haystack.includes(query)) return null;
  const ageSegment = job.age_segment ?? "young_adult";
  return {
    id: `loser:${job.workflow_run_id}:${item.image_id}`,
    source: "generated",
    visibility_scope: "user_private",
    title: `${genderLabel(gender)} · ${AGE_LABEL[ageSegment] ?? ageSegment}`,
    age_segment: ageSegment,
    gender,
    appearance_direction: appearance || null,
    style_tags: item.style_tags,
    image_url: item.image_url,
    display_url: item.display_url,
    thumb_url: item.thumb_url,
    image_id: item.image_id,
    download_filename: item.download_filename,
    is_dual_race_bonus: item.is_dual_race_bonus,
    billing_free: item.billing_free,
    billing_label: item.billing_label,
    billing_exempt_reason: item.billing_exempt_reason,
    created_at: job.created_at,
  };
}

function unsavedLibraryItems(
  jobs: ApparelModelLibraryJob[],
  filters: UnsavedItemFilters,
): ApparelModelLibraryItem[] {
  const items: ApparelModelLibraryItem[] = [];
  for (const job of jobs) {
    if (job.status !== "succeeded" && job.status !== "partial") continue;
    for (const candidate of [...job.items, ...job.candidates]) {
      const item = unsavedLibraryItem(job, candidate, filters);
      if (item) items.push(item);
    }
  }
  return items;
}

function valueForBrowserSource<T>(
  isLoserView: boolean,
  jobsValue: T,
  libraryValue: T,
): T {
  return isLoserView ? jobsValue : libraryValue;
}

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
  headerExtra?: ReactNode;
  className?: string;
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
  const [ageSegment, setAgeSegment] =
    useState<ModelLibraryAgeSegment>(defaultAgeSegment);
  const [appearance, setAppearance] = useState<ModelLibraryAppearance>("all");
  const [source, setSource] = useState<BrowserSource>("all");
  const [query, setQuery] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [mobileFilterOpen, setMobileFilterOpen] = useState(false);
  const [lastUploadedId, setLastUploadedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

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
  const jobsQuery = useApparelModelLibraryJobsQuery(
    { limit: 100 },
    { enabled: isLoserView },
  );
  const syncInfo = libraryQuery.data?.sync;
  const isLoadingItems = isLoserView
    ? jobsQuery.isPending
    : libraryQuery.isPending;

  // 把待入库 items/candidates 适配成 ApparelModelLibraryItem-like 形状
  const items = useMemo<ApparelModelLibraryItem[]>(() => {
    if (isLoserView) {
      return unsavedLibraryItems(jobsQuery.data?.items ?? [], {
        ageSegment,
        appearance,
        query,
      });
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
        filename: item.download_filename ?? undefined,
      })),
    [items],
  );
  const deletableIds = useMemo(
    () =>
      items
        .filter((item) => !item.id.startsWith("loser:"))
        .map((item) => item.id),
    [items],
  );
  const selectedDeletableIds = useMemo(
    () => selectedIds.filter((id) => deletableIds.includes(id)),
    [selectedIds, deletableIds],
  );
  const selectedSet = useMemo(
    () => new Set(selectedDeletableIds),
    [selectedDeletableIds],
  );
  const allVisibleSelected =
    deletableIds.length > 0 && deletableIds.every((id) => selectedSet.has(id));

  const buildLightboxAction = useMemo<null | (() => LightboxAction)>(() => {
    if (mode !== "dialog" || !onSelectItem) return null;
    const itemMap = new Map<string, ApparelModelLibraryItem>(
      items.map((item) => [item.id, item]),
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
        toast.info("预设库刚同步过", {
          description: "已返回最近一次同步结果",
        });
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
  const batchDelete = useDeleteApparelModelLibraryItemsMutation({
    onSuccess: (result) => {
      setSelectedIds([]);
      toast.success("已批量删除", {
        description: `删除 ${result.deleted} 个${
          result.not_found.length ? `，${result.not_found.length} 个未找到` : ""
        }`,
      });
    },
    onError: (err) =>
      toast.error("批量删除失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  // 移动端筛选数：年龄、外貌、来源；非 "all" 计 1
  const activeFilterCount = useMemo(() => {
    let count = 0;
    if (ageSegment !== "all") count += 1;
    if (appearance !== "all") count += 1;
    if (source !== "all") count += 1;
    return count;
  }, [ageSegment, appearance, source]);
  const syncSummary = syncInfo?.last_success_at
    ? `同步 ${formatShortDate(syncInfo.last_success_at)}`
    : "预设 / 收藏 / 上传 / 生成";
  const queryError = valueForBrowserSource(
    isLoserView,
    jobsQuery.error,
    libraryQuery.error,
  );
  const refetchActiveQuery = () => {
    if (isLoserView) return jobsQuery.refetch();
    return libraryQuery.refetch();
  };
  const openLightbox = (item: ApparelModelLibraryItem) => {
    const action = buildLightboxAction?.() ?? null;
    useUiStore
      .getState()
      .openLightboxFromItems(visibleLightboxItems, item.id, action);
  };
  const toggleSelected = (id: string) => {
    setSelectedIds((previous) =>
      previous.includes(id)
        ? previous.filter((selectedId) => selectedId !== id)
        : [...previous, id],
    );
  };
  const changeAgeSegment = (value: ModelLibraryAgeSegment) => {
    setSelectedIds([]);
    setAgeSegment(value);
  };
  const changeAppearance = (value: ModelLibraryAppearance) => {
    setSelectedIds([]);
    setAppearance(value);
  };
  const changeSource = (value: BrowserSource) => {
    setSelectedIds([]);
    setSource(value);
  };
  const changeQuery = (value: string) => {
    setSelectedIds([]);
    setQuery(value);
  };

  return (
    <div className={cn("flex min-h-0 flex-1 flex-col gap-3", className)}>
      <ModelLibraryBrowserLayout
        activeFilterCount={activeFilterCount}
        ageSegment={ageSegment}
        allVisibleSelected={allVisibleSelected}
        appearance={appearance}
        batchDeletePending={batchDelete.isPending}
        deletableIds={deletableIds}
        deleting={deleteItem.isPending || batchDelete.isPending}
        error={queryError}
        headerExtra={headerExtra}
        isLoadingItems={isLoadingItems}
        isLoserView={isLoserView}
        items={items}
        lastUploadedId={lastUploadedId}
        mode={mode}
        query={query}
        selectedDeletableIds={selectedDeletableIds}
        selectedSet={selectedSet}
        selectActionLabel={selectActionLabel}
        showHeader={showHeader}
        showSourceSidebar={showSourceSidebar}
        source={source}
        syncCanRun={Boolean(syncInfo?.can_sync)}
        syncPending={sync.isPending}
        syncSummary={syncSummary}
        onAgeChange={changeAgeSegment}
        onAppearanceChange={changeAppearance}
        onBatchDelete={() => batchDelete.mutate(selectedDeletableIds)}
        onClearSelection={() => setSelectedIds([])}
        onDelete={(id) => deleteItem.mutate(id)}
        onOpenFilter={() => setMobileFilterOpen(true)}
        onOpenLightbox={openLightbox}
        onOpenUpload={() => setUploadOpen(true)}
        onQueryChange={changeQuery}
        onRetry={() => void refetchActiveQuery()}
        onSelectAll={() =>
          setSelectedIds(allVisibleSelected ? [] : deletableIds)
        }
        onSelectItem={onSelectItem}
        onSourceChange={changeSource}
        onSync={() => sync.mutate()}
        onToggleSelected={toggleSelected}
      />
      <ModelLibraryBrowserOverlays
        ageSegment={ageSegment}
        appearance={appearance}
        defaultAgeSegment={defaultAgeSegment}
        mobileFilterOpen={mobileFilterOpen}
        source={source}
        uploadOpen={uploadOpen}
        onAgeChange={changeAgeSegment}
        onAppearanceChange={changeAppearance}
        onCloseMobileFilter={() => setMobileFilterOpen(false)}
        onCloseUpload={() => setUploadOpen(false)}
        onCreated={setLastUploadedId}
        onSourceChange={changeSource}
      />
    </div>
  );
}
