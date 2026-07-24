"use client";

// 代理池：增删改查 + 测延迟 + 健康状态。
// 数据在 system_settings.providers JSON 里和 providers items 共享一行；
// 这里通过 PUT /admin/proxies 仅替换 proxies 数组（items 不动）。

import { useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Edit3,
  Eye,
  EyeOff,
  Network,
  Plus,
  Power,
  PowerOff,
  RefreshCw,
  RotateCcw,
  Save,
  Snowflake,
  Trash2,
  X,
  XCircle,
  Zap,
} from "lucide-react";

import {
  useAdminProxiesQuery,
  useSystemSettingsQuery,
  useTestAllProxiesMutation,
  useTestProxyMutation,
  useUpdateAdminProxiesMutation,
  useUpdateSystemSettingsMutation,
} from "@/lib/queries";
import { ApiError } from "@/lib/apiClient";
import type {
  ProviderProxyIn,
  ProviderProxyType,
  ProxyHealthOut,
  ProxyTestOut,
} from "@/lib/types";
import { Button, IconButton } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { EmptyBlock, ErrorBlock } from "../_components/AdminFeedback";

// ———————————————— Draft 类型 ————————————————

let _seq = 0;
const nextKey = () => ++_seq;

type Draft = ProviderProxyIn & {
  _key: number;
  password: string;
  has_password_on_server: boolean;
};

function emptyDraft(): Draft {
  return {
    _key: nextKey(),
    name: "",
    type: "socks5",
    host: "",
    port: 1080,
    username: "",
    password: "",
    private_key_path: "",
    enabled: true,
    has_password_on_server: false,
  };
}

function toDraft(p: ProxyHealthOut): Draft {
  return {
    _key: nextKey(),
    name: p.name,
    type: p.type,
    host: p.host,
    port: p.port,
    username: p.username ?? "",
    password: "",
    private_key_path: p.private_key_path ?? "",
    enabled: p.enabled,
    has_password_on_server: p.has_password,
  };
}

// ———————————————— 主组件 ————————————————

export function ProxiesPanel() {
  const q = useAdminProxiesQuery();
  const settingsQuery = useSystemSettingsQuery();
  const updateSettingsMut = useUpdateSystemSettingsMutation();
  const updateProxiesMut = useUpdateAdminProxiesMutation();
  const testOne = useTestProxyMutation();
  const testAll = useTestAllProxiesMutation();

  const settings = useMemo(() => {
    const items = settingsQuery.data?.items ?? [];
    const get = (key: string) =>
      items.find((it) => it.key === key)?.value ?? "";
    return {
      test_target: get("proxies.test_target") || "https://api.telegram.org",
      failure_threshold: get("proxies.failure_threshold") || "3",
      cooldown_seconds: get("proxies.cooldown_seconds") || "60",
    };
  }, [settingsQuery.data]);

  const [draftTarget, setDraftTarget] = useState(settings.test_target);
  const [draftThreshold, setDraftThreshold] = useState(settings.failure_threshold);
  const [draftCooldown, setDraftCooldown] = useState(settings.cooldown_seconds);
  // server settings 变化时把 draft 重置为新 server 值（React 19 推荐：render 期间检测变化 + setState）
  const [prevSettings, setPrevSettings] = useState(settings);
  if (prevSettings !== settings) {
    setPrevSettings(settings);
    setDraftTarget(settings.test_target);
    setDraftThreshold(settings.failure_threshold);
    setDraftCooldown(settings.cooldown_seconds);
  }

  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const isEditing = drafts !== null;
  const [confirmDeleteIdx, setConfirmDeleteIdx] = useState<number | null>(null);
  const [editError, setEditError] = useState<string | null>(null);

  const [testResultOf, setTestResultOf] = useState<Record<string, ProxyTestOut>>({});
  const [testingName, setTestingName] = useState<string | null>(null);
  const [bulkError, setBulkError] = useState<string | null>(null);

  const settingsDirty =
    draftTarget !== settings.test_target ||
    draftThreshold !== settings.failure_threshold ||
    draftCooldown !== settings.cooldown_seconds;

  const onSaveSettings = async () => {
    setBulkError(null);
    try {
      await updateSettingsMut.mutateAsync([
        { key: "proxies.test_target", value: draftTarget.trim() },
        { key: "proxies.failure_threshold", value: draftThreshold.trim() },
        { key: "proxies.cooldown_seconds", value: draftCooldown.trim() },
      ]);
    } catch (err) {
      setBulkError(err instanceof Error ? err.message : copy.error.unknown);
    }
  };

  const startEdit = () => {
    setDrafts((q.data?.items ?? []).map(toDraft));
    setEditError(null);
    setConfirmDeleteIdx(null);
  };
  const cancelEdit = () => {
    setDrafts(null);
    setEditError(null);
    setConfirmDeleteIdx(null);
  };

  const addProxy = () => {
    setDrafts((prev) => [...(prev ?? []), emptyDraft()]);
  };
  const updateDraft = (idx: number, patch: Partial<Draft>) => {
    setDrafts((prev) => {
      if (!prev) return prev;
      const next = [...prev];
      next[idx] = { ...next[idx], ...patch };
      return next;
    });
  };
  const removeDraft = (idx: number) => {
    setDrafts((prev) => (prev ?? []).filter((_, i) => i !== idx));
    setConfirmDeleteIdx(null);
  };

  const onSaveProxies = async () => {
    if (!drafts) return;
    setEditError(null);
    // 校验
    const seen = new Set<string>();
    for (const d of drafts) {
      const n = d.name.trim();
      if (!n) {
        setEditError("有代理没填名字");
        return;
      }
      if (seen.has(n)) {
        setEditError(`代理名重复：${n}`);
        return;
      }
      seen.add(n);
      if (!d.host.trim()) {
        setEditError(`「${n}」缺少 host`);
        return;
      }
      if (!Number.isFinite(d.port) || d.port <= 0 || d.port > 65535) {
        setEditError(`「${n}」port 必须在 1-65535`);
        return;
      }
    }
    const payload: ProviderProxyIn[] = drafts.map((d) => ({
      name: d.name.trim(),
      type: d.type,
      host: d.host.trim(),
      port: d.port,
      username: d.username?.trim() || null,
      password: d.password,
      private_key_path: d.private_key_path?.trim() || null,
      enabled: d.enabled,
    }));
    try {
      await updateProxiesMut.mutateAsync(payload);
      setDrafts(null);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : err instanceof Error ? err.message : copy.error.unknown;
      setEditError(msg);
    }
  };

  const onTestOne = async (name: string) => {
    setTestingName(name);
    try {
      const res = await testOne.mutateAsync({ name, target: draftTarget.trim() || undefined });
      setTestResultOf((m) => ({ ...m, [name]: res }));
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : err instanceof Error ? err.message : copy.error.unknown;
      setTestResultOf((m) => ({
        ...m,
        [name]: {
          name,
          target: draftTarget.trim() || settings.test_target,
          latency_ms: -1,
          ok: false,
          error: msg,
        },
      }));
    } finally {
      setTestingName(null);
    }
  };

  const onTestAll = async () => {
    setBulkError(null);
    try {
      const arr = await testAll.mutateAsync(draftTarget.trim() || undefined);
      const m: Record<string, ProxyTestOut> = {};
      arr.forEach((r) => {
        m[r.name] = r;
      });
      setTestResultOf(m);
    } catch (err) {
      setBulkError(err instanceof Error ? err.message : copy.error.unknown);
    }
  };

  return (
    <section className="space-y-5">
      {/* 全局参数 */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-dialog)] p-4 md:p-5 space-y-4">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-[var(--radius-card)] bg-[var(--bg-2)] border border-[var(--border)] flex items-center justify-center shrink-0">
            <Network className="w-4 h-4 text-[var(--fg-2)]" />
          </div>
          <div className="min-w-0">
            <h2 className="type-card-title">代理池</h2>
            <p className="type-caption text-[var(--fg-2)] mt-0.5">
              供应商和 Telegram 机器人共用这套代理。可以在这里增加、修改或删除代理；
              连续失败后会暂停一段时间。
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <Field
            label="测试目标网址"
            hint="点击「测试」时会访问这个地址来估算延迟。"
            value={draftTarget}
            onChange={setDraftTarget}
          />
          <Field
            label="失败几次后停用"
            hint="同一代理连续失败这么多次会进入冷静期，不再被选到。"
            value={draftThreshold}
            onChange={setDraftThreshold}
            inputMode="numeric"
          />
          <Field
            label="停用多少秒后恢复"
            hint="冷静期时长，单位秒。到时间自动重新启用这个代理。"
            value={draftCooldown}
            onChange={setDraftCooldown}
            inputMode="numeric"
          />
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <Button
            variant="primary"
            size="md"
            onClick={onSaveSettings}
            disabled={!settingsDirty || updateSettingsMut.isPending}
            loading={updateSettingsMut.isPending}
          >
            {updateSettingsMut.isPending ? copy.state.saving : "保存这三项"}
          </Button>
          {bulkError && (
            <span className="inline-flex items-center gap-1 type-caption text-danger">
              <AlertCircle className="w-3 h-3" /> {bulkError}
            </span>
          )}
        </div>
      </div>

      {/* 工具栏 */}
      <ProxyToolbar
        isEditing={isEditing}
        hasProxies={(q.data?.items ?? []).length > 0}
        testingAll={testAll.isPending}
        saving={updateProxiesMut.isPending}
        editError={editError}
        onStartEdit={startEdit}
        onTestAll={() => void onTestAll()}
        onRefresh={() => void q.refetch()}
        onSave={() => void onSaveProxies()}
        onCancel={cancelEdit}
        onAdd={addProxy}
      />

      {/* 列表（只读 / 编辑） */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-dialog)] overflow-hidden">
        <ProxyList
          loading={q.isLoading}
          error={q.isError ? q.error : null}
          isEditing={isEditing}
          drafts={drafts}
          proxies={q.data?.items ?? []}
          confirmDeleteIdx={confirmDeleteIdx}
          testResultOf={testResultOf}
          testingName={testingName}
          testing={testOne.isPending}
          onRetry={() => void q.refetch()}
          onChange={updateDraft}
          onDelete={removeDraft}
          onConfirmDelete={setConfirmDeleteIdx}
          onTest={(name) => void onTestOne(name)}
        />
      </div>

      <p className="text-xs text-[var(--fg-2)] px-1">
        提示：测试只会发一个空请求验证代理通路，不会消耗 API 配额。
      </p>
    </section>
  );
}

function ProxyToolbar({
  isEditing,
  hasProxies,
  testingAll,
  saving,
  editError,
  onStartEdit,
  onTestAll,
  onRefresh,
  onSave,
  onCancel,
  onAdd,
}: {
  isEditing: boolean;
  hasProxies: boolean;
  testingAll: boolean;
  saving: boolean;
  editError: string | null;
  onStartEdit: () => void;
  onTestAll: () => void;
  onRefresh: () => void;
  onSave: () => void;
  onCancel: () => void;
  onAdd: () => void;
}) {
  if (!isEditing) {
    return (
      <div className="flex items-center gap-2 flex-wrap">
        <Button
          variant="primary"
          size="md"
          onClick={onStartEdit}
          leftIcon={<Edit3 className="w-3.5 h-3.5" />}
        >
          编辑代理列表
        </Button>
        <Button
          variant="secondary"
          size="md"
          onClick={onTestAll}
          disabled={testingAll || !hasProxies}
          loading={testingAll}
          leftIcon={!testingAll ? <Zap className="w-3.5 h-3.5" /> : undefined}
        >
          {testingAll ? "全部测试中" : "全部测一遍"}
        </Button>
        <Button
          variant="secondary"
          size="md"
          onClick={onRefresh}
          leftIcon={<RefreshCw className="w-3.5 h-3.5" />}
        >
          刷新
        </Button>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <Button
        variant="primary"
        size="md"
        onClick={onSave}
        disabled={saving}
        loading={saving}
        leftIcon={!saving ? <Save className="w-3.5 h-3.5" /> : undefined}
      >
        {saving ? copy.state.saving : "保存代理列表"}
      </Button>
      <Button
        variant="secondary"
        size="md"
        onClick={onCancel}
        disabled={saving}
        leftIcon={<RotateCcw className="w-3.5 h-3.5" />}
      >
        {copy.action.cancel}
      </Button>
      <Button
        variant="secondary"
        size="md"
        onClick={onAdd}
        leftIcon={<Plus className="w-3.5 h-3.5" />}
      >
        加一个代理
      </Button>
      {editError && (
        <span className="inline-flex items-center gap-1 type-caption text-danger">
          <AlertCircle className="w-3 h-3" /> {editError}
        </span>
      )}
    </div>
  );
}

function ProxyDraftList({
  drafts,
  confirmDeleteIdx,
  onChange,
  onDelete,
  onConfirmDelete,
}: {
  drafts: Draft[];
  confirmDeleteIdx: number | null;
  onChange: (index: number, patch: Partial<Draft>) => void;
  onDelete: (index: number) => void;
  onConfirmDelete: (index: number | null) => void;
}) {
  if (drafts.length === 0) {
    return (
      <EmptyBlock
        title="还没有代理"
        description="点「加一个代理」添加第一条。"
      />
    );
  }
  return (
    <ul className="divide-y divide-white/5">
      {drafts.map((draft, index) => (
        <ProxyEditRow
          key={draft._key}
          draft={draft}
          onChange={(patch) => onChange(index, patch)}
          onDelete={() => onDelete(index)}
          confirmingDelete={confirmDeleteIdx === index}
          onConfirmDelete={(value) => onConfirmDelete(value ? index : null)}
        />
      ))}
    </ul>
  );
}

function ProxyReadList({
  proxies,
  testResultOf,
  testingName,
  testing,
  onTest,
}: {
  proxies: ProxyHealthOut[];
  testResultOf: Record<string, ProxyTestOut>;
  testingName: string | null;
  testing: boolean;
  onTest: (name: string) => void;
}) {
  if (proxies.length === 0) {
    return (
      <EmptyBlock
        title="代理池为空"
        description="点「编辑代理列表」添加第一条。"
      />
    );
  }
  return (
    <ul className="divide-y divide-white/5">
      {proxies.map((proxy) => (
        <ProxyViewRow
          key={proxy.name}
          proxy={proxy}
          testResult={testResultOf[proxy.name]}
          testing={testingName === proxy.name && testing}
          onTest={() => onTest(proxy.name)}
        />
      ))}
    </ul>
  );
}

function ProxyList({
  loading,
  error,
  isEditing,
  drafts,
  proxies,
  confirmDeleteIdx,
  testResultOf,
  testingName,
  testing,
  onRetry,
  onChange,
  onDelete,
  onConfirmDelete,
  onTest,
}: {
  loading: boolean;
  error: Error | null;
  isEditing: boolean;
  drafts: Draft[] | null;
  proxies: ProxyHealthOut[];
  confirmDeleteIdx: number | null;
  testResultOf: Record<string, ProxyTestOut>;
  testingName: string | null;
  testing: boolean;
  onRetry: () => void;
  onChange: (index: number, patch: Partial<Draft>) => void;
  onDelete: (index: number) => void;
  onConfirmDelete: (index: number | null) => void;
  onTest: (name: string) => void;
}) {
  if (loading) {
    return (
      <div className="p-6 space-y-3">
        {[1, 2].map((index) => (
          <div
            key={index}
            className="h-16 bg-[var(--bg-2)] rounded-[var(--radius-card)] animate-pulse"
          />
        ))}
      </div>
    );
  }
  if (error) return <ErrorBlock message={error.message || "加载失败"} onRetry={onRetry} />;
  if (isEditing) {
    return (
      <ProxyDraftList
        drafts={drafts ?? []}
        confirmDeleteIdx={confirmDeleteIdx}
        onChange={onChange}
        onDelete={onDelete}
        onConfirmDelete={onConfirmDelete}
      />
    );
  }
  return (
    <ProxyReadList
      proxies={proxies}
      testResultOf={testResultOf}
      testingName={testingName}
      testing={testing}
      onTest={onTest}
    />
  );
}

// ———————————————— 只读行 ————————————————

function ProxyViewRow({
  proxy,
  testResult,
  testing,
  onTest,
}: {
  proxy: ProxyHealthOut;
  testResult?: ProxyTestOut;
  testing: boolean;
  onTest: () => void;
}) {
  const tested =
    testResult ??
    (proxy.last_latency_ms != null
      ? {
          name: proxy.name,
          target: proxy.last_target ?? "",
          latency_ms: proxy.last_latency_ms,
          ok: true,
          error: null,
        }
      : null);

  return (
    <motion.li
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      className="px-4 py-3 md:px-5 md:py-4"
    >
      <div className="flex flex-col md:flex-row md:items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="type-body-sm font-medium text-[var(--fg-0)]">{proxy.name}</span>
            <span className="type-overline px-1.5 py-0.5 rounded bg-[var(--bg-2)] text-[var(--fg-2)] border border-[var(--border)]">
              {proxy.type}
            </span>
            {proxy.enabled ? (
              <span className="inline-flex items-center gap-1 type-overline px-1.5 py-0.5 rounded bg-success-soft text-success border border-success-border">
                <Power className="w-2.5 h-2.5" /> 启用
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 type-overline px-1.5 py-0.5 rounded bg-[var(--bg-2)] text-[var(--fg-3)] border border-[var(--border)]">
                <PowerOff className="w-2.5 h-2.5" /> 禁用
              </span>
            )}
            {proxy.in_cooldown && (
              <span className="inline-flex items-center gap-1 type-overline px-1.5 py-0.5 rounded bg-info-soft text-info border border-info-border">
                <Snowflake className="w-2.5 h-2.5" /> 冷静中
              </span>
            )}
          </div>
          <p className="type-caption text-[var(--fg-2)] font-mono mt-1 break-all">
            {proxy.host}:{proxy.port}
            {proxy.username ? ` (${proxy.username})` : ""}
          </p>
        </div>
        <div className="flex items-center gap-3 md:gap-5 flex-wrap">
          <LatencyBadge tested={tested} testing={testing} />
          <Button
            variant="secondary"
            size="sm"
            onClick={onTest}
            disabled={testing}
            loading={testing}
            leftIcon={!testing ? <Zap className="w-3 h-3" /> : undefined}
          >
            {testing ? "测试中" : "测试"}
          </Button>
        </div>
      </div>
    </motion.li>
  );
}

// ———————————————— 编辑行 ————————————————

function ProxyEditRow({
  draft,
  onChange,
  onDelete,
  confirmingDelete,
  onConfirmDelete,
}: {
  draft: Draft;
  onChange: (patch: Partial<Draft>) => void;
  onDelete: () => void;
  confirmingDelete: boolean;
  onConfirmDelete: (v: boolean) => void;
}) {
  const [showPwd, setShowPwd] = useState(false);

  return (
    <li className="px-4 py-4 md:px-5 md:py-5 space-y-3">
      <div className="flex items-start gap-3 flex-wrap">
        <div className="min-w-0 flex-1 grid grid-cols-2 md:grid-cols-4 gap-3">
          <FieldInline
            label="代理名"
            value={draft.name}
            onChange={(v) => onChange({ name: v })}
            placeholder="比如：备用代理"
            mono
          />
          <FieldSelect
            label="类型"
            value={draft.type}
            onChange={(v) => onChange({ type: v as ProviderProxyType })}
            options={[
              { value: "socks5", label: "SOCKS5" },
              { value: "ssh", label: "SSH 隧道" },
            ]}
          />
          <FieldInline
            label="主机"
            value={draft.host}
            onChange={(v) => onChange({ host: v })}
            placeholder="ip 或域名"
            mono
          />
          <FieldInline
            label="端口"
            value={String(draft.port)}
            onChange={(v) => onChange({ port: Number(v.replace(/[^\d]/g, "")) || 0 })}
            placeholder="1080"
            mono
            inputMode="numeric"
          />
          <FieldInline
            label="用户名（可选）"
            value={draft.username ?? ""}
            onChange={(v) => onChange({ username: v })}
            placeholder="代理需要鉴权时填"
            mono
          />
          <div className="flex flex-col gap-1.5 col-span-2">
            <span className="text-[11px] text-[var(--fg-2)]">
              密码{draft.has_password_on_server ? "（留空保留旧值）" : ""}
            </span>
            <div className="relative">
              <input
                type={showPwd ? "text" : "password"}
                value={draft.password}
                onChange={(e) => onChange({ password: e.target.value })}
                autoComplete="new-password"
                placeholder={draft.has_password_on_server ? "已设置（留空不改）" : "代理需要鉴权时填"}
                className="w-full h-9 pr-9 pl-3 rounded-[var(--radius-control)] bg-[var(--bg-0)]/60 border border-[var(--border)] focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 outline-none text-sm font-mono transition-colors"
              />
              <IconButton
                variant="ghost"
                size="sm"
                onClick={() => setShowPwd((s) => !s)}
                aria-label={showPwd ? "隐藏" : "显示"}
                className="absolute right-2 top-1/2 -translate-y-1/2 w-7 h-7 bg-[var(--bg-2)] hover:bg-[var(--bg-3)]"
              >
                {showPwd ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
              </IconButton>
            </div>
          </div>
          {draft.type === "ssh" && (
            <FieldInline
              label="私钥文件路径（可选）"
              value={draft.private_key_path ?? ""}
              onChange={(v) => onChange({ private_key_path: v })}
              placeholder="/etc/lumen/keys/xxx.pem"
              mono
            />
          )}
        </div>

        <div className="flex flex-col items-end gap-2 shrink-0">
          <Button
            variant="outline"
            size="sm"
            onClick={() => onChange({ enabled: !draft.enabled })}
            leftIcon={draft.enabled ? <Power className="w-3 h-3" /> : <PowerOff className="w-3 h-3" />}
            className={
              draft.enabled
                ? "bg-success-soft text-success border-success-border"
                : "bg-[var(--bg-2)] text-[var(--fg-2)] border-[var(--border)]"
            }
          >
            {draft.enabled ? "启用" : "禁用"}
          </Button>
          <AnimatePresence mode="wait">
            {confirmingDelete ? (
              <motion.div
                key="confirm"
                initial={{ opacity: 0, scale: 0.96 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.96 }}
                className="inline-flex items-center gap-1"
              >
                <Button
                  variant="danger"
                  size="sm"
                  onClick={onDelete}
                  leftIcon={<Trash2 className="w-3 h-3" />}
                >
                  确认删除
                </Button>
                <IconButton
                  variant="secondary"
                  size="sm"
                  onClick={() => onConfirmDelete(false)}
                  aria-label={copy.action.cancel}
                >
                  <X className="w-3.5 h-3.5" />
                </IconButton>
              </motion.div>
            ) : (
              <motion.div
                key="del"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
              >
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onConfirmDelete(true)}
                  leftIcon={<Trash2 className="w-3 h-3" />}
                  className="text-danger hover:bg-danger-soft"
                >
                  {copy.action.delete}
                </Button>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </li>
  );
}

// ———————————————— 共用小组件 ————————————————

function LatencyBadge({
  tested,
  testing,
}: {
  tested: ProxyTestOut | null | undefined;
  testing: boolean;
}) {
  if (testing) {
    return (
      <span className="inline-flex items-center gap-1.5 type-caption text-[var(--fg-2)]">
        <RefreshCw className="w-3 h-3 animate-spin" /> 测试中
      </span>
    );
  }
  if (!tested) {
    return (
      <span className="inline-flex items-center gap-1.5 type-caption text-[var(--fg-2)]">
        <Clock className="w-3 h-3" /> 还未测过
      </span>
    );
  }
  if (!tested.ok) {
    return (
      <span
        className="inline-flex items-center gap-1.5 type-caption text-danger"
        title={tested.error ?? ""}
      >
        <XCircle className="w-3.5 h-3.5" /> 不通
      </span>
    );
  }
  const ms = Math.max(0, tested.latency_ms);
  const color =
    ms < 200 ? "text-success" : ms < 600 ? "text-warning" : "text-danger";
  return (
    <span className={"inline-flex items-center gap-1.5 type-caption " + color}>
      <CheckCircle2 className="w-3.5 h-3.5" />
      <span className="font-mono tabular-nums">{ms.toFixed(0)} ms</span>
    </span>
  );
}

function Field({
  label,
  hint,
  value,
  onChange,
  inputMode,
}: {
  label: string;
  hint: string;
  value: string;
  onChange: (v: string) => void;
  inputMode?: "text" | "numeric";
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="type-caption text-[var(--fg-1)]">{label}</span>
      <input
        type="text"
        value={value}
        inputMode={inputMode}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 px-3 rounded-[var(--radius-control)] bg-[var(--bg-0)]/60 border border-[var(--border)] focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 outline-none text-sm transition-colors"
      />
      <span className="text-[11px] text-[var(--fg-2)] leading-relaxed">{hint}</span>
    </label>
  );
}

function FieldInline({
  label,
  value,
  onChange,
  placeholder,
  mono,
  inputMode,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
  inputMode?: "text" | "numeric";
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11px] text-[var(--fg-2)]">{label}</span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        inputMode={inputMode}
        autoComplete="off"
        className={
          "h-9 px-3 rounded-[var(--radius-control)] bg-[var(--bg-0)]/60 border border-[var(--border)] focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 outline-none text-sm transition-colors " +
          (mono ? "font-mono" : "")
        }
      />
    </label>
  );
}

function FieldSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11px] text-[var(--fg-2)]">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 px-3 rounded-[var(--radius-control)] bg-[var(--bg-0)]/60 border border-[var(--border)] focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 outline-none text-sm transition-colors"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}
