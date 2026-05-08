"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  KeyRound,
  Loader2,
  RefreshCw,
  Trash2,
} from "lucide-react";

import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import {
  ApiError,
  listBindableApiSuppliers,
  listMyApiCredentials,
  putMyApiCredential,
  revokeMyApiCredential,
} from "@/lib/apiClient";

// review §9: 与 /signup 保持一致的错误码 → 文案映射
const BYOK_ERROR_TEXT: Record<string, string> = {
  byok_disabled: "当前未开放 API Key 绑定",
  invalid_api_key: "API Key 无效或被供应商拒绝",
  supplier_unsupported: "供应商或协议不支持",
  model_not_available: "供应商不可用此模型",
  key_rate_limited: "Key 当前被限流，稍后再试",
  supplier_transient_error: "供应商临时错误，请稍后重试",
  validation_timeout: "验证超时",
  validation_wrong_answer: "供应商返回不可信，请检查 Key 与供应商配置",
  invalid_supplier_response: "供应商响应格式不兼容",
  invalid_verification_token: "验证已失效，请重新验证 API Key",
};

export default function ApiKeySettingsPage() {
  const qc = useQueryClient();
  const credentialsQ = useQuery({
    queryKey: ["me", "api-credentials"],
    queryFn: listMyApiCredentials,
    retry: false,
  });
  const suppliersQ = useQuery({
    queryKey: ["me", "api-credentials", "suppliers"],
    queryFn: listBindableApiSuppliers,
    retry: false,
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
  const [saved, setSaved] = useState(false);

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

  const onSave = (e: React.FormEvent) => {
    e.preventDefault();
    setSaved(false);
    setError(null);
    if (!selectedSupplierId) {
      setError("暂无可绑定供应商");
      return;
    }
    if (!apiKey.trim()) {
      setError("请输入 API Key");
      return;
    }
    saveMut.mutate();
  };

  // review §9 / #16: 删除当前 Key 必须二次确认 —— 撤销后任务请求会失败直至重新绑定。
  const handleRevoke = () => {
    if (!active) return;
    const ok =
      typeof window !== "undefined" &&
      window.confirm(
        "撤销当前 API Key？\n注意：撤销后任务请求会失败，直到重新绑定。",
      );
    if (!ok) return;
    revokeMut.mutate(active.id);
  };

  return (
    <SettingsShell title="API Key" subtitle="BYOK" maxWidth="max-w-3xl">
      <div className="space-y-7">
        <header className="hidden items-start justify-between gap-4 md:flex">
          <div>
            <h1 className="type-page-title">API Key</h1>
            <p className="type-body mt-1.5">管理用于上游请求的个人 Key。</p>
          </div>
          <Link
            href="/me"
            className="inline-flex items-center gap-1.5 text-sm text-neutral-400 hover:text-neutral-100"
          >
            <ArrowLeft className="w-4 h-4" />
            返回我的
          </Link>
        </header>

        <section className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 p-5 space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-xl bg-white/5 border border-white/10 flex items-center justify-center">
                <KeyRound className="w-4 h-4" />
              </div>
              <div>
                <p className="text-sm text-neutral-100">当前 Key</p>
                <p className="text-xs text-neutral-500">
                  {credentialsQ.isLoading
                    ? "加载中"
                    : active
                    ? `${active.supplier_name} · ${active.key_hint}`
                    : "未绑定"}
                </p>
              </div>
            </div>
            {active && (
              <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-neutral-300">
                {active.status}
              </span>
            )}
          </div>
          {active?.last_error_code && (
            <div className="rounded-xl border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300">
              {BYOK_ERROR_TEXT[active.last_error_code] ?? active.last_error_code}
            </div>
          )}
          {active && (
            <button
              type="button"
              onClick={handleRevoke}
              disabled={revokeMut.isPending}
              className="inline-flex h-9 items-center gap-1.5 rounded-xl border border-red-500/30 px-3 text-sm text-red-300 hover:bg-red-500/10 disabled:opacity-50"
            >
              {revokeMut.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Trash2 className="w-4 h-4" />
              )}
              删除本地 Key
            </button>
          )}
        </section>

        <form onSubmit={onSave} className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 p-5 space-y-4">
          <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-[var(--fg-2)]">
            <RefreshCw className="w-3.5 h-3.5" />
            绑定或替换
          </div>
          {suppliersQ.isError && (
            <div
              role="alert"
              className="flex items-center justify-between gap-3 rounded-xl border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300"
            >
              <span>供应商列表加载失败</span>
              <button
                type="button"
                onClick={() => void suppliersQ.refetch()}
                disabled={suppliersQ.isFetching}
                className="inline-flex items-center gap-1 rounded-lg border border-white/10 bg-white/5 px-2 py-1 text-xs text-neutral-200 hover:bg-white/10 disabled:opacity-50"
              >
                {suppliersQ.isFetching ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="w-3.5 h-3.5" />
                )}
                重试
              </button>
            </div>
          )}
          <select
            value={selectedSupplierId}
            onChange={(e) => setSupplierId(e.target.value)}
            disabled={suppliersQ.isLoading || suppliers.length === 0 || saveMut.isPending}
            className="w-full h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50"
          >
            {suppliers.length === 0 ? (
              <option value="">暂无可用供应商</option>
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
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={selectedSupplier ? `输入 ${selectedSupplier.name} API Key` : "sk-..."}
            autoComplete="off"
            className="w-full h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-base md:text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50"
          />
          {error && (
            <div className="flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300">
              <AlertCircle className="mt-0.5 w-4 h-4 shrink-0" />
              {error}
            </div>
          )}
          {saved && (
            <div className="flex items-center gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-300">
              <Check className="w-4 h-4" />
              已更新
            </div>
          )}
          <button
            type="submit"
            disabled={saveMut.isPending || suppliers.length === 0}
            className="inline-flex h-10 w-full items-center justify-center gap-2 rounded-xl bg-[var(--color-lumen-amber)] text-sm font-medium text-black disabled:opacity-50 sm:w-auto sm:px-5"
          >
            {saveMut.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <KeyRound className="w-4 h-4" />}
            验证并保存
          </button>
        </form>
      </div>
    </SettingsShell>
  );
}

function apiKeyErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code && BYOK_ERROR_TEXT[err.code]) return BYOK_ERROR_TEXT[err.code];
    return err.message || `请求失败 (HTTP ${err.status})`;
  }
  return err instanceof Error ? err.message : "请求失败";
}
