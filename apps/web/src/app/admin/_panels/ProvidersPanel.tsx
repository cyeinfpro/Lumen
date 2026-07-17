"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  AlertCircle,
  Check,
  ChevronDown,
  ChevronUp,
  Cloud,
  CloudOff,
  GripVertical,
  ImageIcon,
  Loader2,
  Pencil,
  Plus,
  Power,
  PowerOff,
  Server,
  Trash2,
  X,
} from "lucide-react";

import {
  useProbeProvidersMutation,
  usePatchProviderEnabledMutation,
  useProvidersQuery,
  useProviderStatsQuery,
  useUpdateProvidersMutation,
  useUpdateSystemSettingsMutation,
} from "@/lib/queries";
import { ApiError } from "@/lib/apiClient";
import type {
  ProviderItemIn,
  ProviderItemOut,
  ProviderPurpose,
  ProviderProbeResult,
  ProviderProxyIn,
  ProviderProxyOut,
  ProviderStatsItem,
} from "@/lib/types";
import { Button, IconButton } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { EmptyBlock, ErrorBlock } from "../_components/AdminFeedback";
import { ProviderEditActions } from "../_components/ProviderEditActions";

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

const WEIGHT_COLORS = [
  "var(--color-lumen-amber)",
  "#6366f1",
  "#ec4899",
  "#14b8a6",
  "#f97316",
  "#8b5cf6",
  "#06b6d4",
  "#84cc16",
];

const PROVIDER_PURPOSES: Array<{ value: ProviderPurpose; label: string }> = [
  { value: "chat", label: "对话" },
  { value: "image", label: "生图" },
  { value: "embedding", label: "Embedding" },
];

const DEFAULT_PURPOSES: ProviderPurpose[] = ["chat", "image"];

function normalizePurposes(value: ProviderPurpose[] | null | undefined): ProviderPurpose[] {
  const next = value?.filter((purpose) =>
    PROVIDER_PURPOSES.some((option) => option.value === purpose),
  ) ?? [];
  return next.length > 0 ? next : DEFAULT_PURPOSES;
}

function purposeLabel(value: ProviderPurpose): string {
  return PROVIDER_PURPOSES.find((option) => option.value === value)?.label ?? value;
}

// ---------------------------------------------------------------------------
// Draft 类型和工具函数
// ---------------------------------------------------------------------------

type Draft = Omit<ProviderItemIn, "api_key" | "proxy"> & {
  _key: number;
  api_key: string;
  proxy: string | null;
};
type FieldErrors = Record<string, string>;

let _draftSeq = 0;
function nextKey() {
  return ++_draftSeq;
}

function toDraft(p: ProviderItemOut): Draft {
  // BUG-040: 已有 provider 的 api_key 不会被加载到前端 state（设空字符串）。
  // 提交时若 api_key 为空则维持原值。显示使用后端返回的 api_key_hint（已脱敏）。
  return {
    _key: nextKey(),
    name: p.name,
    base_url: p.base_url,
    api_key: "",
    priority: p.priority,
    weight: p.weight,
    enabled: p.enabled,
    purposes: normalizePurposes(p.purposes),
    image_jobs_enabled: p.image_jobs_enabled,
    image_jobs_endpoint: p.image_jobs_endpoint ?? "auto",
    image_jobs_endpoint_lock: p.image_jobs_endpoint_lock ?? false,
    image_jobs_base_url: p.image_jobs_base_url ?? "",
    image_edit_input_transport: p.image_edit_input_transport ?? "url",
    image_concurrency: Math.max(1, p.image_concurrency ?? 1),
    proxy: p.proxy ?? null,
  };
}

function emptyDraft(): Draft {
  return {
    _key: nextKey(),
    name: "",
    base_url: "",
    api_key: "",
    priority: 0,
    weight: 1,
    enabled: true,
    purposes: [...DEFAULT_PURPOSES],
    image_jobs_enabled: false,
    image_jobs_endpoint: "auto",
    image_jobs_endpoint_lock: false,
    image_jobs_base_url: "",
    image_edit_input_transport: "url",
    image_concurrency: 1,
    proxy: null,
  };
}

function providerHasStoredKey(provider: ProviderItemOut | null | undefined): boolean {
  return Boolean(provider?.api_key_hint?.trim());
}

function proxyOutToIn(p: ProviderProxyOut): ProviderProxyIn {
  return {
    name: p.name,
    type: p.type,
    host: p.host,
    port: p.port,
    username: p.username ?? null,
    password: "",
    private_key_path: p.private_key_path ?? null,
    enabled: p.enabled,
  };
}

function providerOutToIn(
  p: ProviderItemOut,
  patch: Partial<Pick<ProviderItemIn, "enabled" | "purposes">> = {},
): ProviderItemIn {
  return {
    name: p.name,
    base_url: p.base_url,
    priority: p.priority,
    weight: Math.max(1, p.weight),
    enabled: patch.enabled ?? p.enabled,
    purposes: patch.purposes ?? normalizePurposes(p.purposes),
    image_jobs_enabled: p.image_jobs_enabled,
    image_jobs_endpoint: p.image_jobs_endpoint ?? "auto",
    image_jobs_endpoint_lock: p.image_jobs_endpoint_lock ?? false,
    image_jobs_base_url: p.image_jobs_base_url ?? "",
    image_edit_input_transport: p.image_edit_input_transport ?? "url",
    image_concurrency: Math.max(1, p.image_concurrency ?? 1),
    proxy: p.proxy ?? null,
  };
}

type PriorityGroup = {
  priority: number;
  items: ProviderItemOut[];
  label: string;
};

function groupByPriority(items: ProviderItemOut[]): PriorityGroup[] {
  const map = new Map<number, ProviderItemOut[]>();
  for (const p of items) {
    const arr = map.get(p.priority) ?? [];
    arr.push(p);
    map.set(p.priority, arr);
  }
  const sorted = [...map.entries()].sort(([a], [b]) => b - a);
  return sorted.map(([priority, items], idx) => ({
    priority,
    items,
    label: idx === 0 && sorted.length > 1 ? "主要" : idx > 0 ? "后备" : "",
  }));
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 1000) return "刚刚";
  if (diff < 60_000) return `${Math.floor(diff / 1000)}s 前`;
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m 前`;
  return `${Math.floor(diff / 3_600_000)}h 前`;
}

function endpointDisplayLabel(value: string | null | undefined): string {
  if (value === "generations") return "生成接口";
  if (value === "responses") return "响应接口";
  return "自动";
}

function editTransportDisplayLabel(value: string | null | undefined): string {
  return value === "file" ? "文件" : "链接";
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export function ProvidersPanel() {
  const q = useProvidersQuery();
  const updateMut = useUpdateProvidersMutation();
  const enabledMut = usePatchProviderEnabledMutation();
  const probeMut = useProbeProvidersMutation();
  const statsQ = useProviderStatsQuery({ enabled: (q.data?.items.length ?? 0) > 0 });
  const settingsMut = useUpdateSystemSettingsMutation();

  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [deleteConfirmIdx, setDeleteConfirmIdx] = useState<number | null>(null);
  const newCardRef = useRef<HTMLDivElement>(null);

  const serverItems = useMemo(() => q.data?.items ?? [], [q.data]);
  const serverProxies = useMemo(() => q.data?.proxies ?? [], [q.data]);
  const source = q.data?.source ?? "none";

  const quickSavePurposes = useCallback(
    (providerName: string, purposes: ProviderPurpose[]) => {
      if (purposes.length === 0) {
        setGlobalError("每个供应商至少需要一个用途");
        return;
      }
      const providerPayload = serverItems.map((provider) =>
        providerOutToIn(
          provider,
          provider.name === providerName ? { purposes } : {},
        ),
      );
      updateMut.mutate(
        { items: providerPayload, proxies: serverProxies.map(proxyOutToIn) },
        {
          onSuccess: () => setSavedAt(Date.now()),
          onError: (err) => {
            setGlobalError(
              err instanceof ApiError
                ? err.message || `保存失败 (HTTP ${err.status})`
                : err.message || "保存失败",
            );
          },
        },
      );
    },
    [serverItems, serverProxies, updateMut],
  );

  const toggleProviderEnabled = useCallback(
    (providerName: string, enabled: boolean) => {
      enabledMut.mutate(
        { name: providerName, enabled },
        {
          onSuccess: () => setSavedAt(Date.now()),
          onError: (err) => {
            setGlobalError(
              err instanceof ApiError
                ? err.message || `切换失败 (HTTP ${err.status})`
                : err.message || "切换失败",
            );
          },
        },
      );
    },
    [enabledMut],
  );

  const probeMap = useMemo(() => {
    const map = new Map<string, ProviderProbeResult>();
    if (probeMut.data) {
      for (const r of probeMut.data.items) map.set(r.name, r);
    }
    return map;
  }, [probeMut.data]);

  const probeTimestamp = probeMut.data?.probed_at ?? null;

  const statsMap = useMemo(() => {
    const map = new Map<string, ProviderStatsItem>();
    if (statsQ.data) {
      for (const s of statsQ.data.items) map.set(s.name, s);
    }
    return map;
  }, [statsQ.data]);

  const autoProbeInterval = statsQ.data?.auto_probe_interval ?? 120;

  // 成功提示自动清除
  useEffect(() => {
    if (savedAt == null) return;
    const t = setTimeout(() => setSavedAt(null), 4000);
    return () => clearTimeout(t);
  }, [savedAt]);

  const cancelEdit = useCallback(() => {
    setDrafts(null);
    setEditingIdx(null);
    setGlobalError(null);
    setDeleteConfirmIdx(null);
  }, []);

  // Escape 键退出
  useEffect(() => {
    if (!drafts) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (editingIdx !== null) {
          setEditingIdx(null);
        } else {
          cancelEdit();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [cancelEdit, drafts, editingIdx]);

  const isEditing = drafts !== null;
  const enabledCount = serverItems.filter((p) => p.enabled).length;
  const healthyCount = probeMut.data
    ? probeMut.data.items.filter((r) => r.ok).length
    : null;

  // ---- 编辑操作 ----

  const startEdit = useCallback(() => {
    setDrafts(serverItems.map(toDraft));
    setEditingIdx(null);
    setGlobalError(null);
    setDeleteConfirmIdx(null);
  }, [serverItems]);

  const addProvider = useCallback(() => {
    const d = emptyDraft();
    setDrafts((prev) => {
      const next = [...(prev ?? []), d];
      // 自动展开新卡片
      setTimeout(() => setEditingIdx(next.length - 1), 0);
      return next;
    });
    setDeleteConfirmIdx(null);
    // 自动滚动到新卡片
    setTimeout(() => {
      newCardRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 100);
  }, []);

  const removeProvider = useCallback((idx: number) => {
    setDrafts((prev) => {
      if (!prev) return prev;
      return prev.filter((_, i) => i !== idx);
    });
    setEditingIdx(null);
    setDeleteConfirmIdx(null);
  }, []);

  const updateDraft = useCallback(
    (idx: number, patch: Partial<Draft>) => {
      setDrafts((prev) => {
        if (!prev) return prev;
        const next = [...prev];
        next[idx] = { ...next[idx], ...patch };
        return next;
      });
    },
    [],
  );

  const moveProvider = useCallback(
    (idx: number, dir: -1 | 1) => {
      setDrafts((prev) => {
        if (!prev) return prev;
        const target = idx + dir;
        if (target < 0 || target >= prev.length) return prev;
        const next = [...prev];
        [next[idx], next[target]] = [next[target], next[idx]];
        return next;
      });
      setEditingIdx((cur) => {
        if (cur === idx) return idx + dir;
        if (cur === idx + dir) return idx;
        return cur;
      });
    },
    [],
  );

  // ---- 保存校验 ----

  const validateAndSave = useCallback(() => {
    if (!drafts) return;
    setGlobalError(null);

    const proxyNames = serverProxies.map((proxy) => proxy.name.trim()).filter(Boolean);

    for (let i = 0; i < drafts.length; i++) {
      const d = drafts[i];
      if (!d.name.trim()) {
        setGlobalError(`供应商 #${i + 1} 缺少名称`);
        setEditingIdx(i);
        return;
      }
      if (!d.base_url.trim()) {
        setGlobalError(`「${d.name}」缺少基础地址`);
        setEditingIdx(i);
        return;
      }
      try {
        const u = new URL(d.base_url.trim());
        if (u.protocol !== "http:" && u.protocol !== "https:") {
          setGlobalError(`「${d.name}」基础地址必须使用 HTTP 或 HTTPS`);
          setEditingIdx(i);
          return;
        }
      } catch {
        setGlobalError(`「${d.name}」基础地址格式不合法`);
        setEditingIdx(i);
        return;
      }
      const existingProvider = serverItems.find((s) => s.name.trim() === d.name.trim());
      const hasStoredKey = providerHasStoredKey(existingProvider);
      if (!d.api_key.trim() && !hasStoredKey && d.enabled) {
        setGlobalError(`「${d.name}」缺少 API 密钥`);
        setEditingIdx(i);
        return;
      }
      if (d.proxy && !proxyNames.includes(d.proxy)) {
        setGlobalError(`「${d.name}」引用了不存在的代理：${d.proxy}`);
        setEditingIdx(i);
        return;
      }
      if (normalizePurposes(d.purposes).length === 0) {
        setGlobalError(`「${d.name}」至少需要一个用途`);
        setEditingIdx(i);
        return;
      }
    }

    const names = drafts.map((d) => d.name.trim());
    const dupes = names.filter((n, i) => names.indexOf(n) !== i);
    if (dupes.length > 0) {
      setGlobalError(
        `名称重复：${[...new Set(dupes)].join(", ")}`,
      );
      return;
    }

    const providerPayload: ProviderItemIn[] = drafts.map((d) => {
      const name = d.name.trim();
      const apiKey = d.api_key.trim();
      const existingProvider = serverItems.find((s) => s.name.trim() === name);
      const hasStoredKey = providerHasStoredKey(existingProvider);
      return {
        name,
        base_url: d.base_url.trim(),
        ...(apiKey || !hasStoredKey ? { api_key: apiKey } : {}),
        priority: d.priority,
        weight: Math.max(1, d.weight),
        enabled: d.enabled,
        purposes: normalizePurposes(d.purposes),
        image_jobs_enabled: d.image_jobs_enabled,
        image_jobs_endpoint: d.image_jobs_endpoint ?? "auto",
        image_jobs_endpoint_lock:
          (d.image_jobs_endpoint ?? "auto") === "auto"
            ? false
            : Boolean(d.image_jobs_endpoint_lock),
        image_jobs_base_url: (d.image_jobs_base_url ?? "").trim(),
        image_edit_input_transport: d.image_edit_input_transport ?? "url",
        image_concurrency: Math.max(
          1,
          Math.min(32, Number(d.image_concurrency ?? 1) || 1)
        ),
        proxy: d.proxy || null,
      };
    });

    // Providers 保存时不再编辑 proxies，直接把服务器现有 proxies 透传回去（password 留空让后端保留旧值）。
    // 代理的增删改在「代理池」标签页做，与本面板解耦。
    const proxyPayload = serverProxies.map(proxyOutToIn);

    updateMut.mutate({ items: providerPayload, proxies: proxyPayload }, {
      onSuccess: () => {
        setSavedAt(Date.now());
        cancelEdit();
      },
      onError: (err) => {
        if (err instanceof ApiError) {
          setGlobalError(err.message || `保存失败 (HTTP ${err.status})`);
        } else {
          setGlobalError(err.message || "保存失败");
        }
      },
    });
  }, [drafts, serverItems, serverProxies, updateMut, cancelEdit]);

  // ---- 探活 ----

  const onProbeAll = useCallback(() => {
    probeMut.mutate(undefined);
  }, [probeMut]);

  const onProbeSingle = useCallback(
    (name: string) => {
      probeMut.mutate([name]);
    },
    [probeMut],
  );

  const onToggleAutoProbe = useCallback(
    (interval: number) => {
      settingsMut.mutate(
        [{ key: "providers.auto_probe_interval", value: String(interval) }],
        { onSuccess: () => void statsQ.refetch() },
      );
    },
    [settingsMut, statsQ],
  );

  // ---- 字段级校验（实时） ----

  const draftErrors = useMemo<Record<number, FieldErrors>>(() => {
    if (!drafts) return {};
    const result: Record<number, FieldErrors> = {};
    const names = drafts.map((d) => d.name.trim());
    for (let i = 0; i < drafts.length; i++) {
      const errs: FieldErrors = {};
      const n = drafts[i].name.trim();
      if (n && names.indexOf(n) !== i) {
        errs.name = "名称重复";
      }
      const url = drafts[i].base_url.trim();
      if (url) {
        try {
          const u = new URL(url);
          if (u.protocol !== "http:" && u.protocol !== "https:") {
            errs.base_url = "必须使用 HTTP 或 HTTPS";
          }
        } catch {
          errs.base_url = "URL 格式不合法";
        }
      }
      if (Object.keys(errs).length > 0) result[i] = errs;
    }
    return result;
  }, [drafts]);

  // ---- 渲染 ----

  const groups = useMemo(() => groupByPriority(serverItems), [serverItems]);

  return (
    <section className="space-y-5 pb-28">
      {/* 顶栏 */}
      <div className="flex flex-col gap-4">
        <div className="flex flex-col sm:flex-row sm:items-center gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2.5">
              <div className="shrink-0 w-9 h-9 rounded-[var(--radius-panel)] bg-[var(--color-lumen-amber)]/15 border border-[var(--color-lumen-amber)]/25 flex items-center justify-center">
                <Server className="w-4 h-4 text-[var(--color-lumen-amber)]" />
              </div>
              <div>
                <h3 className="text-sm font-medium text-[var(--fg-0)]">
                  供应商池
                </h3>
                <p className="text-xs text-[var(--fg-2)] mt-0.5">
                  加权轮询 · 断路器 · 主动探活
                </p>
              </div>
            </div>
          </div>
          {!isEditing && (
            <div className="flex items-center gap-2 flex-wrap">
              <Button
                variant="secondary"
                size="sm"
                onClick={onProbeAll}
                disabled={probeMut.isPending || serverItems.length === 0}
                loading={probeMut.isPending}
                leftIcon={!probeMut.isPending ? <Activity className="w-3 h-3" /> : undefined}
              >
                {probeMut.isPending ? "探活中" : "手动探活"}
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={startEdit}
                leftIcon={<Pencil className="w-3 h-3" />}
              >
                {copy.action.edit}
              </Button>
            </div>
          )}
        </div>

        {/* 统计卡片 */}
        {serverItems.length > 0 && !isEditing && (
          <StatsRow
            total={serverItems.length}
            enabled={enabledCount}
            healthy={healthyCount}
            probing={probeMut.isPending}
            probedAt={probeTimestamp}
            source={source}
          />
        )}
      </div>

      {/* 全局消息 */}
      <AnimatePresence>
        {globalError && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className="rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-4 py-3 type-body-sm text-danger flex items-start gap-2"
          >
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span className="flex-1">{globalError}</span>
            <IconButton
              variant="ghost"
              size="sm"
              onClick={() => setGlobalError(null)}
              aria-label={copy.action.close}
              className="shrink-0"
            >
              <X className="w-3.5 h-3.5" />
            </IconButton>
          </motion.div>
        )}
        {savedAt && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className="rounded-[var(--radius-card)] border border-success-border bg-success-soft px-4 py-3 type-body-sm text-success flex items-center gap-2"
          >
            <Check className="w-4 h-4" /> {copy.state.saved}
          </motion.div>
        )}
      </AnimatePresence>

      {/* 自动探活设置 + 请求统计 */}
      {/* 代理一览已移到「代理池」标签页统一管理；这里不再展示 ProxyOverview。 */}
      {serverItems.length > 0 && !isEditing && (
        <AutoProbeSettings
          interval={autoProbeInterval}
          onChangeInterval={onToggleAutoProbe}
          saving={settingsMut.isPending}
        />
      )}
      {serverItems.length > 0 && !isEditing && statsQ.data && (
        <RequestStatsPanel items={statsQ.data.items} />
      )}

      {/* 流量分配可视化 */}
      {serverItems.length >= 2 && !isEditing && (
        <WeightBar items={serverItems} />
      )}

      {/* 内容区 */}
      {q.isLoading ? (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <div
              key={`skel-${i}`}
              className="h-28 rounded-[var(--radius-dialog)] bg-white/5 animate-pulse"
              style={{ animationDelay: `${i * 80}ms` }}
            />
          ))}
        </div>
      ) : q.isError ? (
        <ErrorBlock
          message={q.error?.message ?? "未知错误"}
          onRetry={() => void q.refetch()}
        />
      ) : isEditing ? (
        <div className="space-y-5">
          {/* 代理增删改已移到「代理池」标签页；编辑 Provider 时通过 dropdown 关联现有代理。 */}
          <DraftList
            drafts={drafts!}
            proxies={serverProxies}
            editingIdx={editingIdx}
            deleteConfirmIdx={deleteConfirmIdx}
            fieldErrors={draftErrors}
            serverKeyHints={new Map(serverItems.map((s) => [s.name.trim(), s.api_key_hint]))}
            newCardRef={newCardRef}
            onEdit={setEditingIdx}
            onUpdate={updateDraft}
            onRemove={removeProvider}
            onMove={moveProvider}
            onDeleteConfirm={setDeleteConfirmIdx}
          />
        </div>
      ) : serverItems.length === 0 ? (
        <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-dialog)] py-16 text-center">
          <div className="flex flex-col items-center gap-4">
            <div className="w-14 h-14 rounded-[var(--radius-dialog)] bg-white/5 border border-[var(--border)] flex items-center justify-center">
              <CloudOff className="w-6 h-6 text-[var(--fg-2)]" />
            </div>
            <div>
              <p className="type-body-sm text-[var(--fg-1)]">还没有供应商</p>
              <p className="type-caption text-[var(--fg-2)] mt-1">
                添加至少一个供应商后，请求会从池里选择可用账号。
              </p>
            </div>
            <Button
              variant="primary"
              size="md"
              onClick={() => {
                startEdit();
                setTimeout(addProvider, 50);
              }}
              leftIcon={<Plus className="w-3.5 h-3.5" />}
            >
              添加首个供应商
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-5">
          {groups.map((group) => (
            <PriorityGroupView
              key={group.priority}
              group={group}
              probeMap={probeMap}
              statsMap={statsMap}
              probing={probeMut.isPending}
              totalGroups={groups.length}
              onProbeSingle={onProbeSingle}
              onToggleEnabled={toggleProviderEnabled}
              onSavePurposes={quickSavePurposes}
              quickSaving={enabledMut.isPending || updateMut.isPending}
            />
          ))}
        </div>
      )}

      <ProviderEditActions
        open={isEditing}
        draftCount={drafts?.length ?? 0}
        saving={updateMut.isPending}
        onAdd={addProvider}
        onCancel={cancelEdit}
        onSave={validateAndSave}
      />
    </section>
  );
}

// ---------------------------------------------------------------------------
// 统计行
// ---------------------------------------------------------------------------

function StatsRow({
  total,
  enabled,
  healthy,
  probing,
  probedAt,
  source,
}: {
  total: number;
  enabled: number;
  healthy: number | null;
  probing: boolean;
  probedAt: string | null;
  source: string;
}) {
  const sourceLabel =
    source === "db"
      ? "数据库"
      : source === "env"
        ? "环境变量"
        : "未配置";
  const sourceIcon =
    source === "db" ? (
      <Server className="w-3 h-3" />
    ) : (
      <Cloud className="w-3 h-3" />
    );

  return (
    <div className="grid grid-cols-3 gap-3">
      <StatCard
        label="供应商"
        value={total}
        sub={
          <span className="inline-flex items-center gap-1 text-[var(--fg-2)]">
            {sourceIcon} {sourceLabel}
          </span>
        }
      />
      <StatCard
        label="已启用"
        value={enabled}
        sub={
          enabled < total ? (
            <span className="text-[var(--fg-2)]">
              {total - enabled} 已禁用
            </span>
          ) : (
            <span className="text-success">全部启用</span>
          )
        }
        accent={enabled === total ? "green" : undefined}
      />
      <StatCard
        label="探活"
        value={
          probing ? (
            <Loader2 className="w-4 h-4 animate-spin text-[var(--color-lumen-amber)]" />
          ) : healthy !== null ? (
            `${healthy}/${enabled}`
          ) : (
            "—"
          )
        }
        sub={
          probedAt ? (
            <span className="text-[var(--fg-2)]">{relativeTime(probedAt)}</span>
          ) : (
            <span className="text-[var(--fg-2)]">未探测</span>
          )
        }
        accent={
          healthy !== null
            ? healthy === enabled
              ? "green"
              : healthy === 0
                ? "red"
                : "amber"
            : undefined
        }
      />
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  accent?: "green" | "red" | "amber";
}) {
  const ring =
    accent === "green"
      ? "border-success-border"
      : accent === "red"
        ? "border-danger-border"
        : accent === "amber"
          ? "border-[var(--color-lumen-amber)]/20"
          : "border-[var(--border)]";

  return (
    <div
      className={`rounded-[var(--radius-panel)] border bg-[var(--bg-1)]/60 backdrop-blur-sm px-4 py-3 ${ring}`}
    >
      <div className="text-[10px] uppercase tracking-wider text-[var(--fg-2)] mb-1">
        {label}
      </div>
      <div className="text-lg font-semibold text-[var(--fg-0)] tabular-nums leading-tight">
        {value}
      </div>
      {sub && <div className="text-[11px] mt-1">{sub}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 流量分配可视化
// ---------------------------------------------------------------------------

function WeightBar({ items }: { items: ProviderItemOut[] }) {
  const enabled = items.filter((p) => p.enabled);
  if (enabled.length < 2) return null;

  // 取最高优先级组
  const maxPriority = Math.max(...enabled.map((p) => p.priority));
  const topGroup = enabled.filter((p) => p.priority === maxPriority);
  if (topGroup.length < 2) return null;

  const totalWeight = topGroup.reduce((s, p) => s + p.weight, 0);

  return (
    <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-panel)] p-4">
      <div className="text-[10px] uppercase tracking-wider text-[var(--fg-2)] mb-2.5">
        流量分配
        {items.some((p) => p.enabled && p.priority < maxPriority) && (
          <span className="normal-case tracking-normal ml-1.5 text-[var(--fg-2)]">
            (Priority {maxPriority} 活跃组)
          </span>
        )}
      </div>
      <div className="flex rounded-[var(--radius-card)] overflow-hidden h-3 gap-px">
        {topGroup.map((p, i) => {
          const pct = (p.weight / totalWeight) * 100;
          return (
            <motion.div
              key={p.name}
              initial={{ width: 0 }}
              animate={{ width: `${pct}%` }}
              transition={{ duration: 0.5, delay: i * 0.08, ease: "easeOut" }}
              className="h-full rounded-[var(--radius-control)]"
              style={{
                backgroundColor: WEIGHT_COLORS[i % WEIGHT_COLORS.length],
                opacity: 0.8,
              }}
              title={`${p.name}: ${Math.round(pct)}%`}
            />
          );
        })}
      </div>
      <div className="flex mt-2 gap-x-4 gap-y-1 flex-wrap">
        {topGroup.map((p, i) => {
          const pct = Math.round((p.weight / totalWeight) * 100);
          return (
            <span key={p.name} className="inline-flex items-center gap-1.5 text-xs">
              <span
                className="w-2 h-2 rounded-[var(--radius-control)] shrink-0"
                style={{
                  backgroundColor: WEIGHT_COLORS[i % WEIGHT_COLORS.length],
                }}
              />
              <span className="text-[var(--fg-1)]">{p.name}</span>
              <span className="text-[var(--fg-2)] tabular-nums">{pct}%</span>
              <span className="text-[var(--fg-2)] tabular-nums">(w={p.weight})</span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 自动探活设置
// ---------------------------------------------------------------------------

const PROBE_INTERVAL_OPTIONS = [
  { label: "关闭", value: 0 },
  { label: "30s", value: 30 },
  { label: "1 分钟", value: 60 },
  { label: "2 分钟", value: 120 },
  { label: "5 分钟", value: 300 },
  { label: "10 分钟", value: 600 },
];

function AutoProbeSettings({
  interval,
  onChangeInterval,
  saving,
}: {
  interval: number;
  onChangeInterval: (v: number) => void;
  saving: boolean;
}) {
  const isOff = interval <= 0;
  return (
    <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-panel)] p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <Activity className="w-4 h-4 text-[var(--fg-1)]" />
          <div>
            <div className="text-xs font-medium text-[var(--fg-0)]">
              自动探活
            </div>
            <div className="text-[11px] text-[var(--fg-2)] mt-0.5">
              {isOff
                ? "已关闭，仅手动探活"
                : `每 ${interval >= 60 ? `${interval / 60} 分钟` : `${interval} 秒`}自动检测`}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {saving && <Loader2 className="w-3 h-3 animate-spin text-[var(--fg-2)]" />}
          <select
            value={interval}
            onChange={(e) => onChangeInterval(Number(e.target.value))}
            disabled={saving}
            className="min-h-[36px] sm:h-8 px-2.5 pr-7 rounded-[var(--radius-card)] bg-[var(--bg-0)]/70 border border-[var(--border)] text-xs text-[var(--fg-0)] focus:outline-none focus:border-[var(--color-lumen-amber)]/50 disabled:opacity-50 transition-colors appearance-none cursor-pointer"
            style={{
              backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23666' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E")`,
              backgroundRepeat: "no-repeat",
              backgroundPosition: "right 8px center",
            }}
          >
            {PROBE_INTERVAL_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 请求统计面板
// ---------------------------------------------------------------------------

function RequestStatsPanel({ items }: { items: ProviderStatsItem[] }) {
  const grandTotal = items.reduce((s, i) => s + i.total, 0);
  if (grandTotal === 0) return null;

  return (
    <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-panel)] p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="text-[10px] uppercase tracking-wider text-[var(--fg-2)]">
          请求统计
        </div>
        <span className="text-[11px] text-[var(--fg-2)] tabular-nums">
          总计 {grandTotal.toLocaleString()} 次请求
        </span>
      </div>
      <div className="space-y-2.5">
        {items.map((s) => {
          const pct = grandTotal > 0 ? (s.total / grandTotal) * 100 : 0;
          const rate = s.total > 0 ? s.success_rate * 100 : 0;
          return (
            <div key={s.name} className="space-y-1.5">
              <div className="flex items-center justify-between text-xs">
                <span className="text-[var(--fg-1)] font-medium">{s.name}</span>
                <div className="flex items-center gap-3 text-[var(--fg-1)]">
                  <span className="tabular-nums">
                    {s.total.toLocaleString()} 次
                  </span>
                  <span className="tabular-nums">
                    流量 {Math.round(pct)}%
                  </span>
                  <span
                    className={`tabular-nums ${
                      rate >= 95
                        ? "text-success"
                        : rate >= 80
                          ? "text-[var(--color-lumen-amber)]"
                          : "text-danger"
                    }`}
                  >
                    成功 {Math.round(rate)}%
                  </span>
                </div>
              </div>
              <div className="flex rounded-[var(--radius-control)] overflow-hidden h-1.5 bg-white/5">
                {s.success > 0 && (
                  <div
                    className="h-full bg-success/70"
                    style={{ width: `${(s.success / s.total) * 100}%` }}
                  />
                )}
                {s.fail > 0 && (
                  <div
                    className="h-full bg-danger/70"
                    style={{ width: `${(s.fail / s.total) * 100}%` }}
                  />
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 优先级分组 + 只读卡片
// ---------------------------------------------------------------------------

function PriorityGroupView({
  group,
  probeMap,
  statsMap,
  probing,
  totalGroups,
  onProbeSingle,
  onToggleEnabled,
  onSavePurposes,
  quickSaving,
}: {
  group: PriorityGroup;
  probeMap: Map<string, ProviderProbeResult>;
  statsMap: Map<string, ProviderStatsItem>;
  probing: boolean;
  totalGroups: number;
  onProbeSingle: (name: string) => void;
  onToggleEnabled: (name: string, enabled: boolean) => void;
  onSavePurposes: (name: string, purposes: ProviderPurpose[]) => void;
  quickSaving: boolean;
}) {
  return (
    <div className="space-y-3">
      {totalGroups > 1 && (
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-wider text-[var(--fg-1)] font-medium whitespace-nowrap">
            Priority {group.priority}
            {group.label && (
              <span className="ml-1.5 text-[var(--fg-2)] normal-case tracking-normal">
                ({group.label})
              </span>
            )}
          </span>
          <div className="flex-1 h-px bg-white/8" />
          <span className="text-[10px] text-[var(--fg-2)] tabular-nums">
            {group.items.length} 个供应商
          </span>
        </div>
      )}
      {group.items.map((p, i) => (
        <ProviderCard
          key={p.name}
          provider={p}
          index={i}
          probe={probeMap.get(p.name)}
          stats={statsMap.get(p.name)}
          probing={probing}
          onProbeSingle={onProbeSingle}
          onToggleEnabled={onToggleEnabled}
          onSavePurposes={onSavePurposes}
          quickSaving={quickSaving}
        />
      ))}
    </div>
  );
}

function ProviderCard({
  provider: p,
  index,
  probe,
  stats,
  probing,
  onProbeSingle,
  onToggleEnabled,
  onSavePurposes,
  quickSaving,
}: {
  provider: ProviderItemOut;
  index: number;
  probe?: ProviderProbeResult;
  stats?: ProviderStatsItem;
  probing: boolean;
  onProbeSingle: (name: string) => void;
  onToggleEnabled: (name: string, enabled: boolean) => void;
  onSavePurposes: (name: string, purposes: ProviderPurpose[]) => void;
  quickSaving: boolean;
}) {
  const purposes = normalizePurposes(p.purposes);

  const togglePurpose = (purpose: ProviderPurpose) => {
    const next = purposes.includes(purpose)
      ? purposes.filter((item) => item !== purpose)
      : [...purposes, purpose];
    if (next.length === 0) return;
    onSavePurposes(p.name, next);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, delay: Math.min(index * 0.04, 0.2) }}
      className={
        "group rounded-[var(--radius-dialog)] border p-5 backdrop-blur-sm transition-colors " +
        (p.enabled
          ? "border-[var(--border)] bg-[var(--bg-1)]/60 hover:border-[var(--border)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)]/30")
      }
    >
      {/* 上部：名称 + 状态 */}
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className={
                "text-sm font-medium " +
                (p.enabled ? "text-[var(--fg-0)]" : "text-[var(--fg-1)]")
              }
            >
              {p.name}
            </span>
            {!p.enabled && (
              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[var(--radius-control)] text-[10px] bg-[var(--fg-2)]/10 text-[var(--fg-2)] border border-[var(--border)]">
                <PowerOff className="w-2.5 h-2.5" /> 已禁用
              </span>
            )}
            {p.image_jobs_enabled && (
              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[var(--radius-control)] text-[10px] bg-info-soft text-info border border-info-border">
                <ImageIcon className="w-2.5 h-2.5" /> 异步生图
              </span>
            )}
          </div>
          <code
            className={
              "text-xs mt-1 block break-all " +
              (p.enabled ? "text-[var(--fg-2)]" : "text-[var(--fg-2)]")
            }
          >
            {p.base_url}
          </code>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {/* 24px tight icon-only button — IconButton sm 高度过大 */}
          <button
            type="button"
            onClick={() => onToggleEnabled(p.name, !p.enabled)}
            disabled={quickSaving}
            className={
              "inline-flex items-center justify-center w-7 h-7 rounded-[var(--radius-control)] border transition-colors " +
              (p.enabled
                ? "bg-success-soft border-success-border text-success hover:bg-success/20"
                : "bg-white/10 border-[var(--border-strong)] text-[var(--fg-2)] hover:bg-white/15")
            }
            aria-label={p.enabled ? "停用供应商" : "启用供应商"}
            title={p.enabled ? "停用供应商" : "启用供应商"}
          >
            {p.enabled ? <Power className="w-3 h-3" /> : <PowerOff className="w-3 h-3" />}
          </button>
          {/* 单个探活按钮 — 24px tight + opacity hover 行为 */}
          <button
            type="button"
            onClick={() => onProbeSingle(p.name)}
            disabled={probing || !p.enabled}
            className="opacity-0 group-hover:opacity-100 focus:opacity-100 inline-flex items-center justify-center w-7 h-7 rounded-[var(--radius-control)] bg-white/5 hover:bg-white/10 border border-[var(--border)] text-[var(--fg-2)] disabled:opacity-30 transition-all"
            aria-label="探活此供应商"
            title="探活此供应商"
          >
            <Activity className="w-3 h-3" />
          </button>
          <ProbeStatusBadge probe={probe} probing={probing} />
        </div>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        {PROVIDER_PURPOSES.map((option) => {
          const checked = purposes.includes(option.value);
          const disabled = quickSaving || (checked && purposes.length === 1);
          return (
            <button
              key={option.value}
              type="button"
              onClick={() => togglePurpose(option.value)}
              disabled={disabled}
              className={
                "inline-flex items-center gap-1.5 rounded-[var(--radius-card)] border px-2 py-1 text-[11px] transition-colors disabled:cursor-not-allowed disabled:opacity-50 " +
                (checked
                  ? "border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/10 text-[var(--color-lumen-amber)]"
                  : "border-[var(--border)] bg-white/[0.03] text-[var(--fg-2)] hover:text-[var(--fg-1)]")
              }
              title={disabled && checked ? "至少保留一个用途" : `切换 ${option.label} 用途`}
            >
              <span
                className={
                  "h-3 w-3 rounded border flex items-center justify-center " +
                  (checked
                    ? "border-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)] text-black"
                    : "border-[var(--border-strong)]")
                }
                aria-hidden
              >
                {checked ? <Check className="h-2.5 w-2.5" /> : null}
              </span>
              {option.label}
            </button>
          );
        })}
      </div>

      {/* 下部：元数据 */}
      <div
        className={
          "flex flex-wrap items-center gap-x-3 gap-y-1 text-xs " +
          (p.enabled ? "text-[var(--fg-1)]" : "text-[var(--fg-2)]")
        }
      >
        <MetaItem
          label="密钥"
          value={p.api_key_hint || "未保存"}
          mono
          color={p.api_key_hint ? undefined : "text-danger"}
        />
        <MetaSep />
        <MetaItem label="优先级" value={String(p.priority)} mono />
        <MetaSep />
        <MetaItem label="权重" value={String(p.weight)} mono />
        <MetaSep />
        <MetaItem
          label="并发"
          value={String(Math.max(1, p.image_concurrency ?? 1))}
          mono
        />
        <MetaSep />
        <MetaItem label="代理" value={p.proxy ?? "直连"} mono />
        {((p.image_jobs_endpoint ?? "auto") !== "auto" ||
          p.image_jobs_enabled) && (
          <>
            <MetaSep />
            <MetaItem
              label="接口"
              value={
                p.image_jobs_endpoint_lock &&
                p.image_jobs_endpoint !== "auto"
                  ? `${endpointDisplayLabel(p.image_jobs_endpoint)} · 已锁定`
                  : endpointDisplayLabel(p.image_jobs_endpoint)
              }
              mono
              color={
                p.image_jobs_endpoint_lock &&
                p.image_jobs_endpoint !== "auto"
                  ? "text-warning"
                  : "text-info"
              }
            />
            {p.image_jobs_base_url && (
              <>
                <MetaSep />
                <MetaItem
                  label="旁路地址"
                  value={p.image_jobs_base_url}
                  mono
                  color="text-info"
                />
              </>
            )}
            {p.image_jobs_enabled && (
              <>
                <MetaSep />
                <MetaItem
                  label="编辑输入"
                  value={editTransportDisplayLabel(p.image_edit_input_transport)}
                  mono
                  color={
                    (p.image_edit_input_transport ?? "url") === "file"
                      ? "text-warning"
                      : "text-info"
                  }
                />
              </>
            )}
          </>
        )}
        {probe?.latency_ms != null && (
          <>
            <MetaSep />
            <MetaItem
              label="延迟"
              value={`${probe.latency_ms}ms`}
              mono
              color={
                probe.latency_ms < 500
                  ? "text-success"
                  : probe.latency_ms < 2000
                    ? "text-[var(--color-lumen-amber)]"
                    : "text-danger"
              }
            />
          </>
        )}
        {stats && stats.total > 0 && (
          <>
            <MetaSep />
            <MetaItem label="请求" value={String(stats.total)} mono />
            <MetaSep />
            <MetaItem
              label="成功率"
              value={`${Math.round(stats.success_rate * 100)}%`}
              mono
              color={
                stats.success_rate >= 0.95
                  ? "text-success"
                  : stats.success_rate >= 0.8
                    ? "text-[var(--color-lumen-amber)]"
                    : "text-danger"
              }
            />
            <MetaSep />
            <MetaItem
              label="流量"
              value={`${Math.round(stats.traffic_pct * 100)}%`}
              mono
            />
          </>
        )}
      </div>
    </motion.div>
  );
}

function MetaItem({
  label,
  value,
  mono,
  color,
}: {
  label: string;
  value: string;
  mono?: boolean;
  color?: string;
}) {
  return (
    <span>
      {label}:{" "}
      <code className={`${mono ? "tabular-nums" : ""} ${color ?? "text-[var(--fg-1)]"}`}>
        {value}
      </code>
    </span>
  );
}

function MetaSep() {
  return <span className="text-[var(--fg-3)]">·</span>;
}

function ProbeStatusBadge({
  probe,
  probing,
}: {
  probe?: ProviderProbeResult;
  probing: boolean;
}) {
  if (probing) {
    return (
      <span className="shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)] text-xs bg-[var(--color-lumen-amber)]/10 text-[var(--color-lumen-amber)] border border-[var(--color-lumen-amber)]/30">
        <Loader2 className="w-3 h-3 animate-spin" />
      </span>
    );
  }
  if (!probe) {
    return (
      <span className="shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)] text-xs bg-[var(--fg-2)]/10 text-[var(--fg-2)] border border-[var(--border)]">
        <span className="w-1.5 h-1.5 rounded-full bg-[var(--fg-2)]" />
        未探测
      </span>
    );
  }
  if (probe.status === "disabled") {
    return (
      <span className="shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)] text-xs bg-[var(--fg-2)]/10 text-[var(--fg-2)] border border-[var(--border)]">
        <PowerOff className="w-3 h-3" /> 跳过
      </span>
    );
  }
  if (probe.ok) {
    return (
      <span className="shrink-0 inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[var(--radius-control)] text-xs bg-success-soft text-success border border-success-border">
        <span className="w-1.5 h-1.5 rounded-full bg-success shadow-[var(--shadow-2)]" />
        健康
        {probe.latency_ms != null && (
          <span className="tabular-nums text-success/80">
            {probe.latency_ms}ms
          </span>
        )}
      </span>
    );
  }
  return (
    <span
      role="alert"
      className="shrink-0 inline-flex max-w-[260px] items-center gap-1.5 px-2 py-0.5 rounded-[var(--radius-control)] text-xs bg-danger-soft text-danger border border-danger-border"
      title={probe.error ?? undefined}
    >
      <span className="w-1.5 h-1.5 rounded-full bg-danger shadow-[var(--shadow-2)]" />
      异常
      {probe.error ? (
        <span role="alert" className="truncate text-danger/85">
          {probe.error}
        </span>
      ) : null}
    </span>
  );
}

// ---------------------------------------------------------------------------
// 编辑态
// ---------------------------------------------------------------------------

function DraftList({
  drafts,
  proxies,
  editingIdx,
  deleteConfirmIdx,
  fieldErrors,
  serverKeyHints,
  newCardRef,
  onEdit,
  onUpdate,
  onRemove,
  onMove,
  onDeleteConfirm,
}: {
  drafts: Draft[];
  proxies: ProviderProxyOut[];
  editingIdx: number | null;
  deleteConfirmIdx: number | null;
  fieldErrors: Record<number, FieldErrors>;
  serverKeyHints: Map<string, string>;
  newCardRef: React.RefObject<HTMLDivElement | null>;
  onEdit: (idx: number | null) => void;
  onUpdate: (idx: number, patch: Partial<Draft>) => void;
  onRemove: (idx: number) => void;
  onMove: (idx: number, dir: -1 | 1) => void;
  onDeleteConfirm: (idx: number | null) => void;
}) {
  if (drafts.length === 0) {
    return (
      <EmptyBlock
        title="暂无供应商"
        description="点击底部「添加」新增一个上游供应商"
      />
    );
  }

  return (
    <div className="space-y-3">
      {drafts.map((d, i) => (
        <DraftCard
          key={d._key}
          ref={i === drafts.length - 1 ? newCardRef : undefined}
          draft={d}
          proxies={proxies}
          index={i}
          total={drafts.length}
          expanded={editingIdx === i}
          showDeleteConfirm={deleteConfirmIdx === i}
          errors={fieldErrors[i]}
          isExisting={serverKeyHints.has(d.name.trim())}
          hasExistingKey={Boolean(serverKeyHints.get(d.name.trim())?.trim())}
          onToggle={() => onEdit(editingIdx === i ? null : i)}
          onUpdate={(patch) => onUpdate(i, patch)}
          onRemove={() => onRemove(i)}
          onMove={(dir) => onMove(i, dir)}
          onDeleteConfirm={(show) => onDeleteConfirm(show ? i : null)}
        />
      ))}
    </div>
  );
}

import { forwardRef } from "react";

function providerApiKeyHint(
  isExisting: boolean,
  hasExistingKey: boolean,
): string {
  if (!isExisting) return "新增供应商必须填写";
  return hasExistingKey
    ? "留空保持原值不变"
    : "当前没有保存密钥，启用前必须填写";
}

function providerApiKeyPlaceholder(
  isExisting: boolean,
  hasExistingKey: boolean,
): string {
  return isExisting && hasExistingKey ? "（留空保持不变）" : "sk-...";
}

const DraftCard = forwardRef<
  HTMLDivElement,
  {
    draft: Draft;
    proxies: ProviderProxyOut[];
    index: number;
    total: number;
    expanded: boolean;
    showDeleteConfirm: boolean;
    errors?: FieldErrors;
    isExisting: boolean;
    hasExistingKey: boolean;
    onToggle: () => void;
    onUpdate: (patch: Partial<Draft>) => void;
    onRemove: () => void;
    onMove: (dir: -1 | 1) => void;
    onDeleteConfirm: (show: boolean) => void;
  }
>(function DraftCard(
  {
    draft,
    proxies,
    index,
    total,
    expanded,
    showDeleteConfirm,
    errors,
    isExisting,
    hasExistingKey,
    onToggle,
    onUpdate,
    onRemove,
    onMove,
    onDeleteConfirm,
  },
  ref,
) {
  const hasErrors = errors && Object.keys(errors).length > 0;
  const nameRef = useRef<HTMLInputElement>(null);

  // 展开时自动 focus 名称字段
  useEffect(() => {
    if (expanded && !draft.name) {
      setTimeout(() => nameRef.current?.focus(), 100);
    }
  }, [expanded, draft.name]);

  return (
    <motion.div
      ref={ref}
      layout="position"
      transition={{ duration: 0.18 }}
      className={
        "rounded-[var(--radius-dialog)] border backdrop-blur-sm transition-colors overflow-hidden " +
        (expanded
          ? hasErrors
            ? "border-danger-border bg-danger-soft"
            : "border-[var(--color-lumen-amber)]/45 bg-[var(--color-lumen-amber)]/[0.04]"
          : "border-[var(--border)] bg-[var(--bg-1)]/60")
      }
    >
      {/* 折叠头 */}
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-white/[0.02] transition-colors"
      >
        <span className="shrink-0 text-[var(--fg-2)]">
          <GripVertical className="w-3.5 h-3.5" />
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xs text-[var(--fg-2)] font-mono tabular-nums shrink-0">
              #{index + 1}
            </span>
            <span className="text-sm font-medium text-[var(--fg-0)] truncate">
              {draft.name || "(未命名)"}
            </span>
            {!draft.enabled && (
              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[var(--radius-control)] text-[10px] bg-[var(--fg-2)]/10 text-[var(--fg-2)] border border-[var(--border)] shrink-0">
                <PowerOff className="w-2.5 h-2.5" /> 禁用
              </span>
            )}
            {hasErrors && (
              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[var(--radius-control)] text-[10px] bg-danger-soft text-danger border border-danger-border shrink-0">
                <AlertCircle className="w-2.5 h-2.5" />
              </span>
            )}
            {!isExisting && draft.name.trim() !== "" && (
              <span className="inline-flex items-center px-1.5 py-0.5 rounded-[var(--radius-control)] text-[10px] bg-info-soft text-info border border-info-border shrink-0">
                新增
              </span>
            )}
          </div>
          {draft.base_url && (
            <code className="text-xs text-[var(--fg-2)] mt-0.5 block truncate">
              {draft.base_url}
            </code>
          )}
          <div className="mt-1 text-[11px] text-[var(--fg-2)]">
            代理：{draft.proxy || "直连"} · 异步生图：
            {draft.image_jobs_enabled ? "支持" : "不支持"} · 用途：
            {normalizePurposes(draft.purposes).map(purposeLabel).join(" / ")}
          </div>
        </div>
        <div className="shrink-0 text-[var(--fg-2)]">
          {expanded ? (
            <ChevronUp className="w-4 h-4" />
          ) : (
            <ChevronDown className="w-4 h-4" />
          )}
        </div>
      </button>

      {/* 展开编辑 */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-5 pb-5 space-y-4 border-t border-[var(--border-subtle)] pt-4">
              {/* 名称 + URL */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <Field
                  label="名称"
                  required
                  error={errors?.name}
                  hint="唯一标识"
                >
                  <input
                    ref={nameRef}
                    type="text"
                    value={draft.name}
                    onChange={(e) => onUpdate({ name: e.target.value })}
                    placeholder="例如：主供应商"
                    className={fieldCls(!!errors?.name)}
                  />
                </Field>
                <Field
                  label="基础地址"
                  required
                  error={errors?.base_url}
                  hint="支持 HTTP/HTTPS，可填内网地址"
                >
                  <input
                    type="url"
                    value={draft.base_url}
                    onChange={(e) => onUpdate({ base_url: e.target.value })}
                    placeholder="http://10.0.0.8:8000/v1"
                    className={fieldCls(!!errors?.base_url)}
                  />
                </Field>
              </div>

              {/* API 密钥 */}
              <Field
                label="API 密钥"
                hint={providerApiKeyHint(isExisting, hasExistingKey)}
                required={!isExisting || !hasExistingKey}
              >
                <input
                  type="password"
                  value={draft.api_key}
                  onChange={(e) => onUpdate({ api_key: e.target.value })}
                  placeholder={providerApiKeyPlaceholder(
                    isExisting,
                    hasExistingKey,
                  )}
                  autoComplete="new-password"
                  className={fieldCls(false)}
                />
              </Field>

              <Field label="用途" hint="先按用途过滤，再按健康度与权重选号">
                <div className="flex flex-wrap gap-2">
                  {PROVIDER_PURPOSES.map((option) => {
                    const purposes = normalizePurposes(draft.purposes);
                    const checked = purposes.includes(option.value);
                    const disabled = checked && purposes.length === 1;
                    return (
                      <button
                        key={option.value}
                        type="button"
                        disabled={disabled}
                        onClick={() => {
                          const next = checked
                            ? purposes.filter((item) => item !== option.value)
                            : [...purposes, option.value];
                          if (next.length > 0) onUpdate({ purposes: next });
                        }}
                        className={
                          "inline-flex min-h-[36px] items-center gap-2 rounded-[var(--radius-panel)] border px-3 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50 " +
                          (checked
                            ? "border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/10 text-[var(--color-lumen-amber)]"
                            : "border-[var(--border)] bg-white/[0.03] text-[var(--fg-2)] hover:text-[var(--fg-1)]")
                        }
                      >
                        <span
                          className={
                            "flex h-3.5 w-3.5 items-center justify-center rounded border " +
                            (checked
                              ? "border-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)] text-black"
                              : "border-[var(--border-strong)]")
                          }
                          aria-hidden
                        >
                          {checked ? <Check className="h-3 w-3" /> : null}
                        </span>
                        {option.label}
                      </button>
                    );
                  })}
                </div>
              </Field>

              {/* 代理选择 */}
              <Field label="代理" hint="供应商可直连或使用一个代理">
                <select
                  value={draft.proxy ?? ""}
                  onChange={(e) => onUpdate({ proxy: e.target.value || null })}
                  className={fieldCls(false)}
                >
                  <option value="">不使用代理</option>
                  {proxies.map((p) => (
                    <option key={p.name} value={p.name.trim()} disabled={!p.name.trim()}>
                      {p.name.trim() || "(未命名代理)"} · {p.type === "ssh" ? "SSH" : "S5"}
                    </option>
                  ))}
                </select>
              </Field>

              {/* 优先级 + 权重 + 并发 + 启用 + 异步生图 */}
              <div className="grid grid-cols-1 md:grid-cols-5 gap-4">
                <Field label="优先级" hint="越大越优先">
                  <input
                    type="number"
                    value={draft.priority}
                    onChange={(e) =>
                      onUpdate({ priority: parseInt(e.target.value, 10) || 0 })
                    }
                    inputMode="numeric"
                    className={fieldCls(false)}
                  />
                </Field>
                <Field label="权重" hint="轮询比例">
                  <input
                    type="number"
                    min={1}
                    value={draft.weight}
                    onChange={(e) =>
                      onUpdate({
                        weight: Math.max(1, parseInt(e.target.value, 10) || 1),
                      })
                    }
                    inputMode="numeric"
                    className={fieldCls(false)}
                  />
                </Field>
                <Field label="并发数" hint="该供应商同时跑的任务上限">
                  <input
                    type="number"
                    min={1}
                    max={32}
                    value={draft.image_concurrency ?? 1}
                    onChange={(e) =>
                      onUpdate({
                        image_concurrency: Math.max(
                          1,
                          Math.min(32, parseInt(e.target.value, 10) || 1)
                        ),
                      })
                    }
                    inputMode="numeric"
                    className={fieldCls(false)}
                  />
                </Field>
                <div className="flex flex-col">
                  <span className="text-xs text-[var(--fg-1)] font-medium mb-1.5">
                    状态
                  </span>
                  <button
                    type="button"
                    onClick={() => onUpdate({ enabled: !draft.enabled })}
                    className={
                      "flex-1 inline-flex items-center gap-1.5 min-h-[44px] sm:h-9 px-3 rounded-[var(--radius-control)] border text-xs transition-colors justify-center " +
                      (draft.enabled
                        ? "bg-success-soft border-success-border text-success hover:bg-success/20"
                        : "bg-white/10 border-[var(--border-strong)] text-[var(--fg-2)] hover:bg-white/15")
                    }
                  >
                    {draft.enabled ? (
                      <>
                        <Power className="w-3 h-3" /> 已启用
                      </>
                    ) : (
                      <>
                        <PowerOff className="w-3 h-3" /> 已禁用
                      </>
                    )}
                  </button>
                </div>
                <div className="flex flex-col">
                  <span className="text-xs text-[var(--fg-1)] font-medium mb-1.5">
                    异步生图
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      onUpdate({
                        image_jobs_enabled: !draft.image_jobs_enabled,
                      })
                    }
                    className={
                      "flex-1 inline-flex items-center gap-1.5 min-h-[44px] sm:h-9 px-3 rounded-[var(--radius-control)] border text-xs transition-colors justify-center " +
                      (draft.image_jobs_enabled
                        ? "bg-info-soft border-info-border text-info hover:bg-info/20"
                        : "bg-white/[0.03] border-[var(--border)] text-[var(--fg-2)] hover:bg-white/[0.06]")
                    }
                  >
                    <ImageIcon className="w-3 h-3" />
                    {draft.image_jobs_enabled ? "支持" : "不支持"}
                  </button>
                  <span className="mt-1 text-[11px] leading-4 text-[var(--fg-2)]">
                    勾选后，图片任务路由才会使用这个供应商。
                  </span>
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 p-3 rounded-[var(--radius-panel)] bg-white/[0.02] border border-[var(--border-subtle)]">
                <div className="flex flex-col">
                  <label className="text-xs text-[var(--fg-1)] font-medium mb-1.5">
                    接口偏好
                  </label>
                  <select
                    value={draft.image_jobs_endpoint ?? "auto"}
                    onChange={(e) =>
                      onUpdate({
                        image_jobs_endpoint:
                          (e.target.value as "auto" | "generations" | "responses") || "auto",
                      })
                    }
                    className="min-h-[44px] sm:h-9 px-3 rounded-[var(--radius-control)] bg-white/[0.03] border border-[var(--border)] text-xs text-[var(--fg-1)] focus:outline-none focus:border-info-border"
                  >
                    <option value="auto">自动（按健康度自适应）</option>
                    <option value="generations">生成接口（/v1/images/generations · /v1/images/edits）</option>
                    <option value="responses">响应接口（/v1/responses + image_generation）</option>
                  </select>
                  <span className="mt-1 text-[11px] leading-4 text-[var(--fg-2)]">
                    适用于异步与同步生图：自动时按健康度在两种接口间切换；锁定后该号只服务对应接口，由其他号兜底对端。
                  </span>
                  {(draft.image_jobs_endpoint ?? "auto") !== "auto" && (
                    <button
                      type="button"
                      onClick={() =>
                        onUpdate({
                          image_jobs_endpoint_lock:
                            !draft.image_jobs_endpoint_lock,
                        })
                      }
                      className={
                        "mt-2 inline-flex items-center gap-1.5 min-h-[36px] sm:h-8 px-3 rounded-[var(--radius-control)] border text-xs transition-colors justify-center " +
                        (draft.image_jobs_endpoint_lock
                          ? "bg-warning-soft border-warning-border text-warning hover:bg-warning/20"
                          : "bg-white/[0.03] border-[var(--border)] text-[var(--fg-2)] hover:bg-white/[0.06]")
                      }
                    >
                      {draft.image_jobs_endpoint_lock
                        ? `已锁定 · 仅服务 ${endpointDisplayLabel(draft.image_jobs_endpoint)}`
                        : "锁定到该接口"}
                    </button>
                  )}
                  {(draft.image_jobs_endpoint ?? "auto") !== "auto" && (
                    <span className="mt-1 text-[11px] leading-4 text-[var(--fg-2)]">
                      锁定后该号不再服务另一个接口：选号阶段直接被过滤，失败也不再回退到对端，由其它号兜底。
                    </span>
                  )}
                </div>
                {draft.image_jobs_enabled && (
                  <div className="flex flex-col">
                    <label className="text-xs text-[var(--fg-1)] font-medium mb-1.5">
                      旁路服务地址（可选）
                    </label>
                    <input
                      type="url"
                      placeholder="留空 = 使用全局任务旁路地址"
                      value={draft.image_jobs_base_url ?? ""}
                      onChange={(e) =>
                        onUpdate({ image_jobs_base_url: e.target.value })
                      }
                      className="min-h-[44px] sm:h-9 px-3 rounded-[var(--radius-control)] bg-white/[0.03] border border-[var(--border)] text-xs text-[var(--fg-1)] placeholder:text-[var(--fg-3)] focus:outline-none focus:border-info-border"
                    />
                    <span className="mt-1 text-[11px] leading-4 text-[var(--fg-2)]">
                      支持给不同供应商指定独立的图片任务旁路服务，例如多区域部署时按供应商路由。
                    </span>
                  </div>
                )}
                {draft.image_jobs_enabled && (
                  <div className="flex flex-col">
                    <label className="text-xs text-[var(--fg-1)] font-medium mb-1.5">
                      编辑接口输入
                    </label>
                    <select
                      value={draft.image_edit_input_transport ?? "url"}
                      onChange={(e) =>
                        onUpdate({
                          image_edit_input_transport:
                            (e.target.value as "url" | "file") || "url",
                        })
                      }
                      className="min-h-[44px] sm:h-9 px-3 rounded-[var(--radius-control)] bg-white/[0.03] border border-[var(--border)] text-xs text-[var(--fg-1)] focus:outline-none focus:border-info-border"
                    >
                      <option value="url">链接（JSON image_url）</option>
                      <option value="file">文件（multipart image[]）</option>
                    </select>
                    <span className="mt-1 text-[11px] leading-4 text-[var(--fg-2)]">
                      只影响图片任务转发 /v1/images/edits；未启用图片任务时直连始终是 multipart 文件。
                    </span>
                  </div>
                )}
              </div>

              {/* 操作栏 */}
              <div className="flex items-center gap-2 pt-3 border-t border-[var(--border-subtle)]">
                <button
                  type="button"
                  onClick={() => onMove(-1)}
                  disabled={index === 0}
                  className="inline-flex items-center gap-1 min-h-[36px] sm:h-7 px-2 rounded-[var(--radius-card)] bg-white/5 hover:bg-white/10 border border-[var(--border)] text-xs text-[var(--fg-1)] disabled:opacity-25 transition-colors"
                >
                  <ChevronUp className="w-3 h-3" /> 上移
                </button>
                <button
                  type="button"
                  onClick={() => onMove(1)}
                  disabled={index === total - 1}
                  className="inline-flex items-center gap-1 min-h-[36px] sm:h-7 px-2 rounded-[var(--radius-card)] bg-white/5 hover:bg-white/10 border border-[var(--border)] text-xs text-[var(--fg-1)] disabled:opacity-25 transition-colors"
                >
                  <ChevronDown className="w-3 h-3" /> 下移
                </button>
                <div className="flex-1" />
                {showDeleteConfirm ? (
                  <motion.div
                    initial={{ opacity: 0, scale: 0.96 }}
                    animate={{ opacity: 1, scale: 1 }}
                    className="inline-flex items-center gap-2"
                  >
                    <span className="type-caption text-[var(--fg-2)]">
                      确认移除?
                    </span>
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={onRemove}
                    >
                      移除
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => onDeleteConfirm(false)}
                    >
                      {copy.action.cancel}
                    </Button>
                  </motion.div>
                ) : (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onDeleteConfirm(true)}
                    leftIcon={<Trash2 className="w-3 h-3" />}
                    className="bg-danger-soft border-danger-border text-danger hover:bg-danger/20"
                  >
                    移除
                  </Button>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
});

// ---------------------------------------------------------------------------
// 表单工具
// ---------------------------------------------------------------------------

function fieldCls(hasError: boolean): string {
  const base =
    "w-full min-h-[44px] sm:h-9 px-3 rounded-[var(--radius-control)] bg-[var(--bg-0)]/70 border text-sm font-mono text-[var(--fg-0)] focus:outline-none focus:ring-2 placeholder:text-[var(--fg-2)] transition-colors";
  if (hasError) {
    return `${base} border-danger-border focus:border-danger-border focus:ring-danger/25`;
  }
  return `${base} border-[var(--border)] focus:border-[var(--color-lumen-amber)]/50 focus:ring-[var(--color-lumen-amber)]/25`;
}

function Field({
  label,
  hint,
  required,
  error,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-baseline gap-1.5 mb-1.5">
        <span className="type-caption text-[var(--fg-1)] font-medium">
          {label}
          {required && <span className="text-danger ml-0.5">*</span>}
        </span>
        {hint && !error && (
          <span className="text-[10px] text-[var(--fg-3)]">{hint}</span>
        )}
        {error && (
          <span className="text-[10px] text-danger flex items-center gap-0.5">
            <AlertCircle className="w-2.5 h-2.5" /> {error}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}
