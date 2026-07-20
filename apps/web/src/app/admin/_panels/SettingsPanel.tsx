"use client";
// Lumen 管理面板：系统设置。
// UI 目标：把工程 key 翻译成可理解的任务语言，同时保留 key 作为排错辅助信息。

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  AlertCircle,
  Check,
  RotateCcw,
  Save,
  Search,
  SlidersHorizontal,
  Sparkles,
} from "lucide-react";

import {
  useAdminModelsQuery,
  useAdminProxiesQuery,
  useProvidersQuery,
  useSystemSettingsQuery,
  useUpdateSystemSettingsMutation,
} from "@/lib/queries";
import { ApiError, getAdminContextHealth } from "@/lib/apiClient";
import type { SystemSettingItem } from "@/lib/types";
import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { ErrorBlock } from "../_components/AdminFeedback";
import {
  ContextHealthBlock,
  SettingsGroup,
  SettingsGroupNav,
  SettingsOverviewCard,
  SettingsSectionHeader,
} from "./settings/views";
import {
  HIDDEN_KEYS,
  IMAGE_CHANNEL_KEY,
  IMAGE_ENGINE_KEY,
  IMAGE_OUTPUT_FORMAT_KEY,
  channelChoiceLabel,
  countByGroup,
  effectiveValue,
  engineChoiceLabel,
  groupSettings,
  isEnvOnlyValue,
  normalizeImageChannel,
  normalizeImageEngine,
  outputFormatChoiceLabel,
  shouldRenderSetting,
  type DependencyState,
  type FilterId,
  type Op,
} from "./settings/model";
import { validateSettingOps } from "./settings/validation";

// Keep these compatibility markers in the entry module for source-level admin gates.
// Hidden update keys: "update.use_proxy_pool", "update.proxy_name".

function clearSubmittedOps(
  currentOps: Record<string, Op>,
  submittedOps: Record<string, Op>,
): Record<string, Op> {
  const next = { ...currentOps };
  for (const [key, submitted] of Object.entries(submittedOps)) {
    const current = next[key];
    if (
      current?.kind === submitted.kind &&
      (current.kind === "clear" ||
        (submitted.kind === "set" && current.value === submitted.value))
    ) {
      delete next[key];
    }
  }
  return next;
}

function clearSubmittedErrors(
  currentErrors: Record<string, string>,
  submittedOps: Record<string, Op>,
): Record<string, string> {
  const next = { ...currentErrors };
  for (const key of Object.keys(submittedOps)) delete next[key];
  return next;
}

const SETTINGS_SKELETON_KEYS = [
  "settings-skeleton-summary",
  "settings-skeleton-image",
  "settings-skeleton-context",
] as const;

export function SettingsPanel() {
  const q = useSystemSettingsQuery();
  const updateMut = useUpdateSystemSettingsMutation();
  const adminModelsQ = useAdminModelsQuery({ retry: false });
  const providersQ = useProvidersQuery({ retry: false });
  const proxiesQ = useAdminProxiesQuery({ retry: false });
  const contextHealthQ = useQuery({
    queryKey: ["admin", "context", "health"],
    queryFn: getAdminContextHealth,
    retry: false,
  });

  const [ops, setOps] = useState<Record<string, Op>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [activeGroup, setActiveGroup] = useState<FilterId>("all");
  const [search, setSearch] = useState("");

  useEffect(() => {
    if (savedAt == null) return;
    const t = setTimeout(() => setSavedAt(null), 4000);
    return () => clearTimeout(t);
  }, [savedAt]);

  const items = useMemo(
    () =>
      (q.data?.items ?? []).filter((it) => !HIDDEN_KEYS.has(it.key)),
    [q.data],
  );
  const itemByKey = useMemo(() => {
    const map = new Map<string, SystemSettingItem>();
    for (const item of items) map.set(item.key, item);
    return map;
  }, [items]);
  const imageEngine = effectiveValue(
    itemByKey.get(IMAGE_ENGINE_KEY),
    ops[IMAGE_ENGINE_KEY],
    "responses",
  );
  const imageChannel = effectiveValue(
    itemByKey.get(IMAGE_CHANNEL_KEY),
    ops[IMAGE_CHANNEL_KEY],
    "auto",
  );
  const imageOutputFormat = effectiveValue(
    itemByKey.get(IMAGE_OUTPUT_FORMAT_KEY),
    ops[IMAGE_OUTPUT_FORMAT_KEY],
    "jpeg",
  );
  const compressionSetting = itemByKey.get("context.compression_enabled");
  const compressionEnabled =
    isEnvOnlyValue(compressionSetting, ops["context.compression_enabled"]) ||
    effectiveValue(
      compressionSetting,
      ops["context.compression_enabled"],
      "0",
    ) === "1";
  const imageCaptionSetting = itemByKey.get("context.image_caption_enabled");
  const imageCaptionEnabled =
    isEnvOnlyValue(imageCaptionSetting, ops["context.image_caption_enabled"]) ||
    effectiveValue(
      imageCaptionSetting,
      ops["context.image_caption_enabled"],
      "1",
    ) === "1";
  const dependencyState = useMemo<DependencyState>(
    () => ({
      imageChannel,
      compressionEnabled,
      imageCaptionEnabled,
    }),
    [compressionEnabled, imageCaptionEnabled, imageChannel],
  );
  const visibleItems = useMemo(
    () => items.filter((item) => shouldRenderSetting(item.key, dependencyState)),
    [items, dependencyState],
  );
  const providerStatus = useMemo(() => {
    const providers = providersQ.data?.items ?? [];
    const total = providers.filter((provider) => provider.enabled).length;
    const jobs = providers.filter(
      (provider) => provider.enabled && provider.image_jobs_enabled,
    ).length;
    return {
      total,
      jobs,
      label: providersQ.isLoading
        ? "读取中"
        : total > 0
          ? `${jobs} / ${total} 个供应商已启用异步任务`
          : "未配置供应商",
      compact:
        providersQ.isLoading || total === 0 ? "自动" : `自动 · ${jobs}/${total} 启用`,
    };
  }, [providersQ.data, providersQ.isLoading]);
  const dirtyCount = Object.keys(ops).length;
  const groups = useMemo(
    () => groupSettings(visibleItems, activeGroup, search),
    [activeGroup, visibleItems, search],
  );
  const visibleCount = groups.reduce((sum, group) => sum + group.items.length, 0);
  const groupCounts = useMemo(() => countByGroup(visibleItems), [visibleItems]);
  const overview = useMemo(() => {
    const defaultModel = effectiveValue(
      itemByKey.get("upstream.default_model"),
      ops["upstream.default_model"],
      "gpt-5.5",
    );
    return {
      defaultModelLabel: defaultModel || "gpt-5.5",
      engineLabel: engineChoiceLabel(imageEngine),
      channelLabel:
        normalizeImageChannel(imageChannel) === "auto"
          ? providerStatus.compact
          : channelChoiceLabel(imageChannel),
      formatLabel: outputFormatChoiceLabel(imageOutputFormat),
      compressionLabel: compressionEnabled ? "已开启" : "已关闭",
    };
  }, [
    compressionEnabled,
    imageChannel,
    imageEngine,
    imageOutputFormat,
    itemByKey,
    ops,
    providerStatus.compact,
  ]);

  const setOp = (key: string, op: Op | undefined) => {
    setOps((prev) => {
      const next = { ...prev };
      if (!op) delete next[key];
      else next[key] = op;
      return next;
    });
    setFieldErrors((prev) => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  const validateAll = (): {
    ok: boolean;
    payload: { key: string; value: string }[];
  } => {
    const result = validateSettingOps(ops);
    setFieldErrors(result.errors);
    return { ok: result.ok, payload: result.payload };
  };

  const onSave = () => {
    setGlobalError(null);
    setSavedAt(null);
    const { ok, payload } = validateAll();
    if (!ok) {
      setGlobalError("存在错误项");
      return;
    }
    if (payload.length === 0) return;

    const submittedOps = ops;
    updateMut.mutate(payload, {
      onSuccess: () => {
        setSavedAt(Date.now());
        setOps((currentOps) => clearSubmittedOps(currentOps, submittedOps));
        setFieldErrors((currentErrors) =>
          clearSubmittedErrors(currentErrors, submittedOps),
        );
      },
      onError: (err) => {
        if (err instanceof ApiError) {
          setGlobalError(err.message || `保存失败 (HTTP ${err.status})`);
        } else {
          setGlobalError(err.message || "保存失败");
        }
      },
    });
  };

  const onResetAll = () => {
    setOps({});
    setFieldErrors({});
    setGlobalError(null);
    setSavedAt(null);
  };

  return (
    <section className="space-y-6 pb-24">
      <SettingsOverviewCard
        overview={overview}
        dirtyCount={dirtyCount}
        visibleCount={visibleItems.length}
      />

      <AnimatePresence>
        {normalizeImageEngine(imageEngine) === "dual_race" && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            role="alert"
            className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-4 py-3 type-body-sm text-danger"
          >
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            双并发会同时启动两条生图路径，成功率和速度更激进，但单次任务可能消耗双倍配额。
          </motion.div>
        )}
        {globalError && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            role="alert"
            className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-4 py-3 type-body-sm text-danger"
          >
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            {globalError}
          </motion.div>
        )}
        {savedAt && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            role="status"
            className="flex items-center gap-2 rounded-[var(--radius-card)] border border-success-border bg-success-soft px-4 py-3 type-body-sm text-success"
          >
            <Check className="h-4 w-4" /> {copy.state.saved}
          </motion.div>
        )}
      </AnimatePresence>

      <SettingsSectionHeader
        icon={SlidersHorizontal}
        title="配置项"
        description="按业务场景分组编辑。左侧选分类，右侧只显示相关设置。"
        badge={`${visibleCount} 项显示`}
      />

      <div className="grid gap-4 lg:grid-cols-[250px_minmax(0,1fr)]">
        <aside
          data-testid="admin-settings-group-menu"
          className="self-start rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/64 p-3 shadow-[var(--shadow-1)] backdrop-blur-sm lg:sticky lg:top-4 lg:max-h-[calc(100dvh-2rem)] lg:overflow-y-auto lg:pr-2 lg:scrollbar-thin"
        >
          <SettingsGroupNav
            activeGroup={activeGroup}
            totalCount={visibleItems.length}
            groupCounts={groupCounts}
            onChange={setActiveGroup}
          />
          <div className="mt-3 border-t border-[var(--border-subtle)] pt-3">
            <label className="relative block">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="搜索设置或技术名"
                className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/70 pl-9 pr-3 type-body-sm text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-2)] focus:border-accent-border focus:ring-2 focus:ring-accent/20"
              />
            </label>
            <p className="mt-2 type-caption text-[var(--fg-2)]">
              已修改 {dirtyCount} 项。修改后使用底部保存条统一提交。
            </p>
          </div>
        </aside>

        <div className="min-w-0">
          {q.isLoading ? (
            <div className="space-y-3">
              {SETTINGS_SKELETON_KEYS.map((key, i) => (
                <div
                  key={key}
                  className="h-32 animate-pulse rounded-[var(--radius-card)] bg-[var(--bg-1)]/70"
                  style={{ animationDelay: `${i * 80}ms` }}
                />
              ))}
            </div>
          ) : q.isError ? (
            <ErrorBlock
              message={q.error?.message ?? "未知错误"}
              onRetry={() => void q.refetch()}
            />
          ) : items.length === 0 ? (
            <div className="flex flex-col items-center gap-3 rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 py-14 text-center type-body-sm text-[var(--fg-2)] backdrop-blur-sm">
              <Sparkles className="h-5 w-5 text-[var(--fg-2)]" />
              没有可配置项
            </div>
          ) : visibleCount === 0 ? (
            <div className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 px-4 py-12 text-center type-body-sm text-[var(--fg-2)]">
              没有找到匹配的设置
            </div>
          ) : (
            <div className="space-y-8">
              {groups.map((group) => (
                <SettingsGroup
                  key={group.id}
                  group={group}
                  ops={ops}
                  fieldErrors={fieldErrors}
                  dependencyState={dependencyState}
                  modelsQuery={{
                    isLoading: adminModelsQ.isLoading,
                    isError: adminModelsQ.isError,
                    errorMessage: adminModelsQ.error?.message,
                    models: Array.isArray(adminModelsQ.data?.models)
                      ? adminModelsQ.data.models.map((model) => model.id)
                      : [],
                  }}
                  providerStatus={providerStatus}
                  updateProxyOptions={proxiesQ.data?.items ?? []}
                  onChange={setOp}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      <SettingsSectionHeader
        icon={Activity}
        title="长对话摘要状态"
        description="只读监控放在这里，系统更新控制台已集中到健康页。"
        badge="只读"
      />

      <ContextHealthBlock
        loading={contextHealthQ.isLoading}
        error={contextHealthQ.error}
        onRetry={() => void contextHealthQ.refetch()}
        data={contextHealthQ.data}
      />

      <AnimatePresence>
        {dirtyCount > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 30 }}
            transition={{ duration: 0.2 }}
            className="fixed bottom-0 left-0 right-0 z-40 max-w-full px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:bottom-4 sm:left-1/2 sm:right-auto sm:w-auto sm:max-w-[calc(100vw-2rem)] sm:-translate-x-1/2 sm:px-0 sm:pb-4"
          >
            <div className="grid grid-cols-2 items-stretch gap-2 rounded-[var(--radius-dialog)] border border-accent-border bg-[var(--bg-1)]/95 px-3 py-2.5 shadow-[var(--shadow-3)] backdrop-blur-xl sm:flex sm:items-center sm:gap-3 sm:px-4">
              <span className="col-span-2 inline-flex items-center gap-1.5 type-caption text-[var(--fg-1)] sm:col-span-1 sm:whitespace-nowrap">
                <span className="h-1.5 w-1.5 rounded-full bg-accent shadow-[var(--shadow-amber)]" />
                <span className="font-mono tabular-nums">{dirtyCount}</span>
                <span>项待保存</span>
              </span>
              <div className="hidden flex-1 sm:block sm:flex-none" />
              <Button
                variant="secondary"
                size="sm"
                onClick={onResetAll}
                disabled={updateMut.isPending}
                leftIcon={<RotateCcw className="h-3 w-3" />}
              >
                放弃
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={onSave}
                disabled={updateMut.isPending}
                loading={updateMut.isPending}
                leftIcon={!updateMut.isPending ? <Save className="h-3 w-3" /> : undefined}
              >
                {updateMut.isPending ? copy.state.saving : "保存全部"}
              </Button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}
