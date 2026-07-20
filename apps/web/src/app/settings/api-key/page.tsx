"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { format } from "date-fns";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  Clock3,
  KeyRound,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from "lucide-react";

import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { Button, Card, ConfirmDialog } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import {
  ApiError,
  getMe,
  listBindableApiSuppliers,
  listMyApiCredentials,
  probeMyApiCredential,
  putMyApiCredential,
  revokeMyApiCredential,
} from "@/lib/apiClient";
import type { UserApiCredentialOut } from "@/lib/types";

// review §9: 与 /signup 保持一致的错误码 → 文案映射（业务专用，不归入 copy.ts）
const BYOK_ERROR_TEXT: Record<string, string> = {
  byok_disabled: "未开放绑定",
  invalid_api_key: "Key 被拒绝",
  supplier_unsupported: "供应商不支持",
  model_not_available: "模型不可用",
  key_rate_limited: "Key 被限流",
  supplier_transient_error: "供应商临时错误",
  validation_timeout: "验证超时",
  validation_wrong_answer: "供应商响应不可信",
  invalid_supplier_response: "响应格式不兼容",
  invalid_verification_token: "验证已失效",
};

function getApiKeySaveError(
  supplierId: string,
  apiKey: string,
): string | null {
  if (!supplierId) return "无可绑定供应商";
  if (!apiKey.trim()) return "Key 为空";
  return null;
}

function shouldShowWalletAccount(
  hasAccount: boolean,
  isByok: boolean,
): boolean {
  return hasAccount && !isByok;
}

type BindableApiSupplier = Awaited<
  ReturnType<typeof listBindableApiSuppliers>
>["items"][number];

export default function ApiKeySettingsPage() {
  const qc = useQueryClient();
  const meQ = useQuery({ queryKey: ["me"], queryFn: getMe, retry: false });
  const isByok = meQ.data?.account_mode === "byok";
  const credentialsQ = useQuery({
    queryKey: ["me", "api-credentials"],
    queryFn: listMyApiCredentials,
    retry: false,
    enabled: isByok,
  });
  const suppliersQ = useQuery({
    queryKey: ["me", "api-credentials", "suppliers"],
    queryFn: listBindableApiSuppliers,
    retry: false,
    enabled: isByok,
  });
  const credentials = credentialsQ.data?.items ?? [];
  const active = credentials.find((item) => item.status === "active") ?? credentials[0];
  const suppliers = useMemo(
    () => suppliersQ.data?.items ?? [],
    [suppliersQ.data?.items],
  );
  const [supplierId, setSupplierId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [probeMessage, setProbeMessage] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);

  const selectedSupplierId = supplierId || suppliers[0]?.id || "";
  const selectedSupplier = useMemo(
    () => suppliers.find((supplier) => supplier.id === selectedSupplierId),
    [selectedSupplierId, suppliers],
  );

  const saveMut = useMutation({
    mutationFn: () => putMyApiCredential(selectedSupplierId, apiKey.trim()),
    onSuccess: async () => {
      setApiKey("");
      setSaved(true);
      setProbeMessage(null);
      setError(null);
      await qc.invalidateQueries({ queryKey: ["me", "api-credentials"] });
    },
    onError: (err) => {
      setSaved(false);
      setError(apiKeyErrorText(err));
    },
  });

  const revokeMut = useMutation({
    mutationFn: (credentialId: string) => revokeMyApiCredential(credentialId),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["me", "api-credentials"] });
    },
    onError: (err) => setError(apiKeyErrorText(err)),
  });

  const probeMut = useMutation({
    mutationFn: (credentialId: string) => probeMyApiCredential(credentialId),
    onSuccess: async (credential) => {
      setSaved(false);
      setError(null);
      setProbeMessage(credentialHealthText(credential));
      await qc.invalidateQueries({ queryKey: ["me", "api-credentials"] });
    },
    onError: (err) => {
      setSaved(false);
      setProbeMessage(null);
      setError(apiKeyErrorText(err));
    },
  });

  const onSave = (e: React.FormEvent) => {
    e.preventDefault();
    setSaved(false);
    setError(null);
    const validationError = getApiKeySaveError(selectedSupplierId, apiKey);
    if (validationError) {
      setError(validationError);
      return;
    }
    saveMut.mutate();
  };

  // review §9 / #16: 删除当前 Key 必须二次确认 —— 撤销后任务请求会失败直至重新绑定。
  const handleRevoke = () => {
    if (!active) return;
    setRevokeOpen(true);
  };

  const confirmRevoke = () => {
    if (!active) return;
    revokeMut.mutate(active.id);
    setRevokeOpen(false);
  };

  if (shouldShowWalletAccount(Boolean(meQ.data), isByok)) {
    return (
      <SettingsShell title="API Key" subtitle="Wallet" maxWidth="max-w-3xl">
        <Card variant="subtle" padding="lg" className="space-y-3">
          <p className="type-card-title">钱包账号</p>
          <p className="type-body">
            当前账号使用平台供应商和钱包扣费，不支持绑定个人 API Key。
          </p>
          <Link
            href="/me/wallet"
            className="inline-flex min-h-11 items-center rounded-[var(--radius-control)] border border-[var(--border)] px-3 text-xs text-[var(--fg-0)] hover:bg-[var(--bg-2)]"
          >
            查看钱包
          </Link>
        </Card>
      </SettingsShell>
    );
  }

  return (
    <SettingsShell title="API Key" subtitle="BYOK" maxWidth="max-w-3xl">
      <div className="space-y-5 pb-4 sm:space-y-7">
        <header className="hidden items-start justify-between gap-4 md:flex">
          <div>
            <h1 className="type-page-title">API Key</h1>
            <p className="type-body mt-1.5">管理用于上游请求的个人 Key。</p>
          </div>
          <Link
            href="/me"
            className="inline-flex min-h-9 items-center gap-1.5 px-2 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)]"
          >
            <ArrowLeft className="w-4 h-4" />
            返回我的
          </Link>
        </header>

        <ApiCredentialHealthCard
          active={active}
          loading={credentialsQ.isLoading}
          probeMessage={probeMessage}
          probing={probeMut.isPending}
          revoking={revokeMut.isPending}
          onProbe={() => {
            if (active) probeMut.mutate(active.id);
          }}
          onRevoke={handleRevoke}
        />

        <ApiCredentialForm
          suppliers={suppliers}
          selectedSupplier={selectedSupplier}
          selectedSupplierId={selectedSupplierId}
          apiKey={apiKey}
          error={error}
          saved={saved}
          loadingSuppliers={suppliersQ.isLoading}
          suppliersError={suppliersQ.isError}
          fetchingSuppliers={suppliersQ.isFetching}
          saving={saveMut.isPending}
          onSubmit={onSave}
          onRetrySuppliers={() => void suppliersQ.refetch()}
          onSupplierChange={setSupplierId}
          onApiKeyChange={setApiKey}
        />
      </div>

      <ConfirmDialog
        open={revokeOpen}
        onOpenChange={setRevokeOpen}
        title="撤销 API Key？"
        description="撤销后任务将失败"
        confirmText={copy.action.confirm}
        cancelText={copy.action.cancel}
        tone="danger"
        confirming={revokeMut.isPending}
        onConfirm={confirmRevoke}
      />
    </SettingsShell>
  );
}

function ApiCredentialHealthCard({
  active,
  loading,
  probeMessage,
  probing,
  revoking,
  onProbe,
  onRevoke,
}: {
  active: UserApiCredentialOut | undefined;
  loading: boolean;
  probeMessage: string | null;
  probing: boolean;
  revoking: boolean;
  onProbe: () => void;
  onRevoke: () => void;
}) {
  const summary = loading
    ? copy.state.loading
    : active
      ? `${active.supplier_name} · ${active.key_hint}`
      : "未绑定";
  const lastError = active?.last_error_code
    ? (BYOK_ERROR_TEXT[active.last_error_code] ?? active.last_error_code)
    : null;

  return (
    <Card variant="subtle" padding="lg" className="space-y-4 max-sm:p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]">
            <KeyRound className="w-4 h-4" />
          </div>
          <div>
            <p className="type-body-sm text-[var(--fg-0)]">当前 Key 健康</p>
            <p className="type-caption text-[var(--fg-2)]">{summary}</p>
          </div>
        </div>
        {active ? (
          <span className="rounded-full border border-[var(--border)] bg-white/5 px-2.5 py-1 type-caption text-[var(--fg-1)]">
            {active.status}
          </span>
        ) : null}
      </div>
      {active ? (
        <div className="grid gap-2 sm:grid-cols-3">
          <HealthMeta
            icon={<ShieldCheck className="w-3.5 h-3.5" />}
            label="最近通过"
            value={formatDateTime(active.last_verified_at)}
          />
          <HealthMeta
            icon={<AlertCircle className="w-3.5 h-3.5" />}
            label="最近失败"
            value={formatDateTime(active.last_failed_at)}
          />
          <HealthMeta
            icon={<Clock3 className="w-3.5 h-3.5" />}
            label="限流恢复"
            value={formatDateTime(active.rate_limited_until)}
          />
        </div>
      ) : null}
      {lastError ? (
        <div
          role="alert"
          aria-live="assertive"
          className="rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-[var(--danger-fg)]"
        >
          {lastError}
        </div>
      ) : null}
      {probeMessage ? (
        <div className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 py-2 type-body-sm text-[var(--fg-1)]">
          {probeMessage}
        </div>
      ) : null}
      {active ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={onProbe}
            disabled={probing || active.status !== "active"}
            loading={probing}
            leftIcon={!probing ? <RefreshCw className="w-4 h-4" /> : undefined}
          >
            重新检测
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={onRevoke}
            disabled={revoking}
            loading={revoking}
            leftIcon={!revoking ? <Trash2 className="w-4 h-4" /> : undefined}
            className="border-danger-border text-danger hover:bg-danger-soft"
          >
            {copy.action.delete}
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

function ApiCredentialForm({
  suppliers,
  selectedSupplier,
  selectedSupplierId,
  apiKey,
  error,
  saved,
  loadingSuppliers,
  suppliersError,
  fetchingSuppliers,
  saving,
  onSubmit,
  onRetrySuppliers,
  onSupplierChange,
  onApiKeyChange,
}: {
  suppliers: BindableApiSupplier[];
  selectedSupplier: BindableApiSupplier | undefined;
  selectedSupplierId: string;
  apiKey: string;
  error: string | null;
  saved: boolean;
  loadingSuppliers: boolean;
  suppliersError: boolean;
  fetchingSuppliers: boolean;
  saving: boolean;
  onSubmit: (event: React.FormEvent) => void;
  onRetrySuppliers: () => void;
  onSupplierChange: (value: string) => void;
  onApiKeyChange: (value: string) => void;
}) {
  const supplierControlsDisabled =
    loadingSuppliers || suppliers.length === 0 || saving;
  const keyPlaceholder = selectedSupplier
    ? `${selectedSupplier.name} API Key`
    : "sk-...";

  return (
    <form
      onSubmit={onSubmit}
      className="space-y-4 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-4 sm:p-6"
    >
      <div className="flex items-center gap-2 type-overline">
        <RefreshCw className="w-3.5 h-3.5" />
        绑定或替换
      </div>
      {suppliersError ? (
        <div
          role="alert"
          className="flex items-center justify-between gap-3 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-[var(--danger-fg)]"
        >
          <span>供应商列表加载失败</span>
          <Button
            variant="outline"
            size="sm"
            onClick={onRetrySuppliers}
            disabled={fetchingSuppliers}
            loading={fetchingSuppliers}
            leftIcon={
              !fetchingSuppliers ? (
                <RefreshCw className="w-3.5 h-3.5" />
              ) : undefined
            }
          >
            {copy.action.retry}
          </Button>
        </div>
      ) : null}
      <select
        value={selectedSupplierId}
        onChange={(event) => onSupplierChange(event.target.value)}
        disabled={supplierControlsDisabled}
        className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-base focus:outline-none focus:border-[var(--accent)]/50 md:h-10 md:text-sm"
      >
        {suppliers.length === 0 ? (
          <option value="">无可用供应商</option>
        ) : (
          suppliers.map((supplier) => (
            <option key={supplier.id} value={supplier.id}>
              {supplier.name} · {supplier.validation_model}
            </option>
          ))
        )}
      </select>
      <input
        type="password"
        value={apiKey}
        onChange={(event) => onApiKeyChange(event.target.value)}
        placeholder={keyPlaceholder}
        autoComplete="off"
        className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-base focus:outline-none focus:border-[var(--accent)]/50 md:h-10 md:text-sm"
      />
      {error ? (
        <div
          role="alert"
          aria-live="assertive"
          className="flex items-start gap-2 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-[var(--danger-fg)]"
        >
          <AlertCircle className="mt-0.5 w-4 h-4 shrink-0" />
          {error}
        </div>
      ) : null}
      {saved ? (
        <div className="flex items-center gap-2 rounded-[var(--radius-control)] border border-success-border bg-success-soft px-3 py-2 type-body-sm text-success">
          <Check className="w-4 h-4" />
          {copy.state.saved}
        </div>
      ) : null}
      <div className="sticky bottom-0 -mx-4 border-t border-[var(--border-subtle)] bg-[var(--bg-1)]/95 px-4 pb-[max(0px,env(safe-area-inset-bottom))] pt-3 backdrop-blur-xl sm:static sm:mx-0 sm:border-0 sm:bg-transparent sm:p-0 sm:backdrop-blur-none">
        <Button
          type="submit"
          variant="primary"
          size="md"
          disabled={saving || suppliers.length === 0}
          loading={saving}
          leftIcon={!saving ? <KeyRound className="w-4 h-4" /> : undefined}
          fullWidth
          className="sm:w-auto"
        >
          验证并保存
        </Button>
      </div>
    </form>
  );
}

function apiKeyErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code && BYOK_ERROR_TEXT[err.code]) return BYOK_ERROR_TEXT[err.code];
    return err.message || `请求失败 · HTTP ${err.status}`;
  }
  return err instanceof Error ? err.message : "请求失败";
}

function credentialHealthText(credential: UserApiCredentialOut): string {
  if (credential.last_error_code) {
    const label = BYOK_ERROR_TEXT[credential.last_error_code] ?? credential.last_error_code;
    return `检测完成：${label}。后续生成会继续提示这个 Key 的状态。`;
  }
  return "检测完成：供应商已接受当前 Key。";
}

function formatDateTime(value: string | null): string {
  if (!value) return "无记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return format(date, "yyyy-MM-dd HH:mm:ss");
}

function HealthMeta({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 px-3 py-2">
      <div className="flex items-center gap-1.5 type-caption text-[var(--fg-2)]">
        {icon}
        {label}
      </div>
      <div className="mt-1 truncate type-body-sm text-[var(--fg-0)]" title={value}>
        {value}
      </div>
    </div>
  );
}
