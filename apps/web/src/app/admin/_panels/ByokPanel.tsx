"use client";

import { useMemo, useState } from "react";
import {
  AlertCircle,
  Check,
  Loader2,
  Plus,
  Save,
  Server,
  ShieldCheck,
  TestTube2,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  createApiSupplier,
  getByokSettings,
  listApiSuppliers,
  patchApiSupplier,
  patchByokSettings,
  probeApiSupplier,
} from "@/lib/apiClient";
import type {
  ApiSupplierTemplateIn,
  ApiSupplierTemplateOut,
  ByokPurpose,
  ByokSettingsPatchIn,
} from "@/lib/types";

type SupplierDraft = ApiSupplierTemplateIn & { probe_key: string };

const PURPOSES: ByokPurpose[] = ["chat", "image", "embedding"];

// 与后端 ApiSupplierTemplateIn 校验范围对齐（review §9 / #15）。
const TIMEOUT_MIN_MS = 1000;
const TIMEOUT_MAX_MS = 60_000;
const CONCURRENCY_MIN = 1;
const CONCURRENCY_MAX = 32;

const EMPTY_SUPPLIER: SupplierDraft = {
  name: "",
  slug: "",
  base_url: "",
  enabled: true,
  public_signup_enabled: false,
  user_bind_enabled: true,
  purposes: ["chat"],
  validation_model: "gpt-5.4",
  default_chat_model: "gpt-5.4",
  fast_chat_model: "gpt-5.4-mini",
  validation_timeout_ms: 15000,
  proxy_name: "",
  text_concurrency_per_key: 4,
  image_concurrency_per_key: 1,
  capabilities_jsonb: {},
  probe_key: "",
};

function clampInt(raw: string | number, min: number, max: number): number {
  const n = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(n)) return min;
  return Math.max(min, Math.min(max, Math.floor(n)));
}

function validateBaseUrl(v: string): string | null {
  if (!v.trim()) return "必填";
  try {
    const url = new URL(v);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return "必须是 http(s)";
    }
    if (url.username || url.password) return "URL 不能包含账号密码";
    return null;
  } catch {
    return "URL 格式错误";
  }
}

function togglePurpose(
  purposes: ByokPurpose[],
  target: ByokPurpose,
): ByokPurpose[] {
  if (purposes.includes(target)) {
    const next = purposes.filter((p) => p !== target);
    // 至少保留一个 purpose（与后端 schemas 校验保持一致）
    return next.length > 0 ? next : purposes;
  }
  return [...purposes, target];
}

export function ByokPanel() {
  const qc = useQueryClient();
  const settingsQ = useQuery({
    queryKey: ["admin", "byok-settings"],
    queryFn: getByokSettings,
    retry: false,
  });
  const suppliersQ = useQuery({
    queryKey: ["admin", "byok-suppliers"],
    queryFn: listApiSuppliers,
    retry: false,
  });

  const [settingsDraft, setSettingsDraft] = useState<ByokSettingsPatchIn>({});
  const [newSupplier, setNewSupplier] = useState<SupplierDraft>(EMPTY_SUPPLIER);
  const [newSupplierUrlError, setNewSupplierUrlError] = useState<string | null>(
    null,
  );
  const [supplierDrafts, setSupplierDrafts] = useState<Record<string, SupplierDraft>>({});
  const [supplierUrlErrors, setSupplierUrlErrors] = useState<
    Record<string, string | null>
  >({});
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [probeResult, setProbeResult] = useState<Record<string, string>>({});

  const saveSettingsMut = useMutation({
    mutationFn: () => patchByokSettings(settingsDraft),
    onSuccess: async () => {
      setSaved("系统设置已更新");
      await qc.invalidateQueries({ queryKey: ["admin", "byok-settings"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  const createMut = useMutation({
    mutationFn: () => createApiSupplier(supplierDraftToCreateBody(newSupplier)),
    onSuccess: async () => {
      setNewSupplier(EMPTY_SUPPLIER);
      setNewSupplierUrlError(null);
      setSaved("供应商已创建");
      await qc.invalidateQueries({ queryKey: ["admin", "byok-suppliers"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  const supplierIds = useMemo(
    () => suppliersQ.data?.items.map((supplier) => supplier.id) ?? [],
    [suppliersQ.data],
  );

  const patchSupplier = useMutation({
    mutationFn: (payload: { id: string; body: SupplierDraft }) =>
      patchApiSupplier(payload.id, supplierDraftToPatchBody(payload.body)),
    onSuccess: async () => {
      setSaved("供应商已更新");
      await qc.invalidateQueries({ queryKey: ["admin", "byok-suppliers"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  const probeMut = useMutation({
    mutationFn: (payload: { id: string; api_key: string }) =>
      probeApiSupplier(payload.id, payload.api_key),
    onSuccess: (res, vars) => {
      setProbeResult((current) => ({
        ...current,
        [vars.id]: res.ok
          ? `通过 · ${res.latency_ms}ms`
          : `${res.error_code ?? "probe_failed"} · ${res.latency_ms}ms`,
      }));
    },
    onError: (err, vars) => {
      setProbeResult((current) => ({
        ...current,
        [vars.id]: errorText(err),
      }));
    },
  });

  const settings = settingsQ.data;
  const suppliers = suppliersQ.data?.items ?? [];

  // review §9 / #27 / #9: BYOK 总开关关闭时其他三个开关需 disable + 提示
  const modeOn = Boolean(
    settingsDraft.mode_enabled ?? settings?.mode_enabled ?? false,
  );
  const SETTING_TOGGLES: Array<{
    key: keyof ByokSettingsPatchIn;
    label: string;
    requiresMode: boolean;
  }> = [
    { key: "mode_enabled", label: "BYOK 总开关", requiresMode: false },
    { key: "byok_signup_enabled", label: "公开注册", requiresMode: true },
    {
      key: "byok_signup_bypasses_allowlist",
      label: "绕过白名单",
      requiresMode: true,
    },
    {
      key: "fallback_to_admin_provider",
      label: "管理员兜底",
      requiresMode: true,
    },
  ];

  return (
    <div className="space-y-6">
      <section className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 p-5 space-y-4">
        <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-[var(--fg-2)]">
          <ShieldCheck className="w-3.5 h-3.5" />
          BYOK 开关
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {SETTING_TOGGLES.map(({ key, label, requiresMode }) => {
            const disabled = requiresMode && !modeOn;
            const draftRecord = settingsDraft as Record<
              string,
              boolean | undefined
            >;
            const settingsRecord = settings as
              | Record<string, boolean | undefined>
              | undefined;
            const checked = Boolean(
              draftRecord[key] ?? settingsRecord?.[key as string],
            );
            return (
              <label
                key={key}
                className={
                  "flex items-center justify-between gap-3 rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2 " +
                  (disabled ? "opacity-60" : "")
                }
              >
                <span className="text-sm text-neutral-200 flex flex-col">
                  {label}
                  {disabled && (
                    <span className="text-[10px] text-neutral-500 mt-0.5">
                      需要先开启 BYOK 模式
                    </span>
                  )}
                </span>
                <input
                  type="checkbox"
                  disabled={disabled}
                  checked={checked}
                  onChange={(e) =>
                    setSettingsDraft((current) => ({
                      ...current,
                      [key]: e.target.checked,
                    }))
                  }
                />
              </label>
            );
          })}
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <input
            value={settingsDraft.validation_model ?? settings?.validation_model ?? ""}
            onChange={(e) =>
              setSettingsDraft((current) => ({ ...current, validation_model: e.target.value }))
            }
            placeholder="验证模型"
            className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <input
            type="number"
            min={TIMEOUT_MIN_MS}
            max={TIMEOUT_MAX_MS}
            value={
              settingsDraft.validation_timeout_ms ??
              settings?.validation_timeout_ms ??
              15000
            }
            onChange={(e) =>
              setSettingsDraft((current) => ({
                ...current,
                validation_timeout_ms: clampInt(
                  e.target.value,
                  TIMEOUT_MIN_MS,
                  TIMEOUT_MAX_MS,
                ),
              }))
            }
            className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <input
            type="number"
            min={60}
            max={3600}
            value={
              settingsDraft.pending_token_ttl_seconds ??
              settings?.pending_token_ttl_seconds ??
              900
            }
            onChange={(e) =>
              setSettingsDraft((current) => ({
                ...current,
                pending_token_ttl_seconds: clampInt(e.target.value, 60, 3600),
              }))
            }
            className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
        </div>
        <button
          type="button"
          onClick={() => saveSettingsMut.mutate()}
          disabled={saveSettingsMut.isPending}
          className="inline-flex h-10 items-center gap-2 rounded-xl bg-[var(--color-lumen-amber)] px-4 text-sm font-medium text-black disabled:opacity-50"
        >
          {saveSettingsMut.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          保存系统设置
        </button>
      </section>

      <section className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 p-5 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-[var(--fg-2)]">
            <Plus className="w-3.5 h-3.5" />
            新供应商
          </div>
          <div className="text-xs text-[var(--fg-2)]">
            {supplierIds.length} 个模板
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <input
            value={newSupplier.name}
            onChange={(e) =>
              setNewSupplier((current) => ({ ...current, name: e.target.value }))
            }
            placeholder="名称"
            className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <input
            value={newSupplier.slug ?? ""}
            onChange={(e) =>
              setNewSupplier((current) => ({ ...current, slug: e.target.value }))
            }
            placeholder="slug"
            className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <div className="md:col-span-2 space-y-1">
            <input
              value={newSupplier.base_url}
              onChange={(e) => {
                setNewSupplier((current) => ({
                  ...current,
                  base_url: e.target.value,
                }));
                if (newSupplierUrlError) setNewSupplierUrlError(null);
              }}
              onBlur={(e) => setNewSupplierUrlError(validateBaseUrl(e.target.value))}
              placeholder="https://api.example.com"
              className={
                "w-full h-10 rounded-xl border bg-[var(--bg-0)] px-3 text-sm " +
                (newSupplierUrlError
                  ? "border-red-500/60"
                  : "border-[var(--border)]")
              }
            />
            {newSupplierUrlError && (
              <p className="text-xs text-red-300">{newSupplierUrlError}</p>
            )}
          </div>
        </div>
        {/* review §9 / #13: 必填的 purposes 多选 chip */}
        <div className="space-y-2">
          <label className="text-xs uppercase tracking-wider text-[var(--fg-2)]">
            用途 (purposes)
          </label>
          <div className="flex gap-2 flex-wrap">
            {PURPOSES.map((p) => {
              const active = newSupplier.purposes.includes(p);
              return (
                <button
                  key={p}
                  type="button"
                  onClick={() =>
                    setNewSupplier((current) => ({
                      ...current,
                      purposes: togglePurpose(current.purposes, p),
                    }))
                  }
                  className={
                    "px-2.5 py-1 rounded-lg border text-xs transition-colors " +
                    (active
                      ? "bg-[var(--color-lumen-amber)] text-black border-[var(--color-lumen-amber)]"
                      : "bg-white/[0.03] text-neutral-300 border-white/10 hover:bg-white/[0.08]")
                  }
                >
                  {p}
                </button>
              );
            })}
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <label className="flex items-center justify-between rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2 text-sm">
            注册
            <input
              type="checkbox"
              checked={newSupplier.public_signup_enabled}
              onChange={(e) =>
                setNewSupplier((current) => ({
                  ...current,
                  public_signup_enabled: e.target.checked,
                }))
              }
            />
          </label>
          <label className="flex items-center justify-between rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2 text-sm">
            绑定
            <input
              type="checkbox"
              checked={newSupplier.user_bind_enabled}
              onChange={(e) =>
                setNewSupplier((current) => ({
                  ...current,
                  user_bind_enabled: e.target.checked,
                }))
              }
            />
          </label>
          <label className="flex items-center justify-between rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2 text-sm">
            启用
            <input
              type="checkbox"
              checked={newSupplier.enabled}
              onChange={(e) =>
                setNewSupplier((current) => ({
                  ...current,
                  enabled: e.target.checked,
                }))
              }
            />
          </label>
          <input
            value={newSupplier.proxy_name ?? ""}
            onChange={(e) =>
              setNewSupplier((current) => ({ ...current, proxy_name: e.target.value }))
            }
            placeholder="proxy_name"
            className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <label className="space-y-1">
            <span className="text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
              验证超时 (ms, 1000-60000)
            </span>
            <input
              type="number"
              min={TIMEOUT_MIN_MS}
              max={TIMEOUT_MAX_MS}
              value={newSupplier.validation_timeout_ms}
              onChange={(e) =>
                setNewSupplier((current) => ({
                  ...current,
                  validation_timeout_ms: clampInt(
                    e.target.value,
                    TIMEOUT_MIN_MS,
                    TIMEOUT_MAX_MS,
                  ),
                }))
              }
              className="w-full h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            />
          </label>
          <label className="space-y-1">
            <span className="text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
              text 并发 (1-32)
            </span>
            <input
              type="number"
              min={CONCURRENCY_MIN}
              max={CONCURRENCY_MAX}
              value={newSupplier.text_concurrency_per_key}
              onChange={(e) =>
                setNewSupplier((current) => ({
                  ...current,
                  text_concurrency_per_key: clampInt(
                    e.target.value,
                    CONCURRENCY_MIN,
                    CONCURRENCY_MAX,
                  ),
                }))
              }
              className="w-full h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            />
          </label>
          <label className="space-y-1">
            <span className="text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
              image 并发 (1-32)
            </span>
            <input
              type="number"
              min={CONCURRENCY_MIN}
              max={CONCURRENCY_MAX}
              value={newSupplier.image_concurrency_per_key}
              onChange={(e) =>
                setNewSupplier((current) => ({
                  ...current,
                  image_concurrency_per_key: clampInt(
                    e.target.value,
                    CONCURRENCY_MIN,
                    CONCURRENCY_MAX,
                  ),
                }))
              }
              className="w-full h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            />
          </label>
        </div>
        <button
          type="button"
          onClick={() => {
            const urlErr = validateBaseUrl(newSupplier.base_url);
            if (urlErr) {
              setNewSupplierUrlError(urlErr);
              return;
            }
            createMut.mutate();
          }}
          disabled={createMut.isPending}
          className="inline-flex h-10 items-center gap-2 rounded-xl border border-white/10 bg-white/[0.05] px-4 text-sm disabled:opacity-50"
        >
          {createMut.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Server className="w-4 h-4" />}
          创建模板
        </button>
      </section>

      <div className="space-y-4">
        {suppliers.map((supplier) => (
          <SupplierCard
            key={supplier.id}
            supplier={supplier}
            draft={supplierDrafts[supplier.id] ?? supplierToDraft(supplier)}
            urlError={supplierUrlErrors[supplier.id] ?? null}
            onChange={(next) =>
              setSupplierDrafts((current) => ({ ...current, [supplier.id]: next }))
            }
            onUrlBlur={(err) =>
              setSupplierUrlErrors((current) => ({
                ...current,
                [supplier.id]: err,
              }))
            }
            onSave={() => {
              const body = supplierDrafts[supplier.id] ?? supplierToDraft(supplier);
              const urlErr = validateBaseUrl(body.base_url);
              if (urlErr) {
                setSupplierUrlErrors((current) => ({
                  ...current,
                  [supplier.id]: urlErr,
                }));
                return;
              }
              patchSupplier.mutate({ id: supplier.id, body });
            }}
            onProbe={() =>
              probeMut.mutate({
                id: supplier.id,
                api_key: (supplierDrafts[supplier.id]?.probe_key ?? "").trim(),
              })
            }
            probeLabel={probeResult[supplier.id]}
            busy={patchSupplier.isPending || probeMut.isPending}
          />
        ))}
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300">
          <AlertCircle className="mt-0.5 w-4 h-4 shrink-0" />
          {error}
        </div>
      )}
      {saved && (
        <div className="flex items-center gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-sm text-emerald-300">
          <Check className="w-4 h-4" />
          {saved}
        </div>
      )}
    </div>
  );
}

function SupplierCard({
  supplier,
  draft,
  urlError,
  onChange,
  onUrlBlur,
  onSave,
  onProbe,
  probeLabel,
  busy,
}: {
  supplier: ApiSupplierTemplateOut;
  draft: SupplierDraft;
  urlError: string | null;
  onChange: (next: SupplierDraft) => void;
  onUrlBlur: (err: string | null) => void;
  onSave: () => void;
  onProbe: () => void;
  probeLabel?: string;
  busy: boolean;
}) {
  const set = (patch: Partial<SupplierDraft>) => onChange({ ...draft, ...patch });
  return (
    <section className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 p-5 space-y-4">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h3 className="text-sm text-neutral-100">{supplier.name}</h3>
          <p className="text-xs text-neutral-500">
            {supplier.slug} · {supplier.base_url}
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-neutral-400">
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1">
            active {supplier.active_credentials}
          </span>
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1">
            {supplier.validation_model}
          </span>
        </div>
      </div>
      {supplier.recent_error_counts && Object.keys(supplier.recent_error_counts).length > 0 && (
        <p className="text-xs text-neutral-500">
          {Object.entries(supplier.recent_error_counts)
            .map(([key, value]) => `${key}:${value}`)
            .join(" · ")}
        </p>
      )}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <input
          value={draft.name}
          onChange={(e) => set({ name: e.target.value })}
          className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
        />
        <input
          value={draft.slug ?? ""}
          onChange={(e) => set({ slug: e.target.value })}
          className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
        />
        <div className="md:col-span-2 space-y-1">
          <input
            value={draft.base_url}
            onChange={(e) => {
              set({ base_url: e.target.value });
              if (urlError) onUrlBlur(null);
            }}
            onBlur={(e) => onUrlBlur(validateBaseUrl(e.target.value))}
            className={
              "w-full h-10 rounded-xl border bg-[var(--bg-0)] px-3 text-sm " +
              (urlError ? "border-red-500/60" : "border-[var(--border)]")
            }
          />
          {urlError && <p className="text-xs text-red-300">{urlError}</p>}
        </div>
        <input
          value={draft.validation_model}
          onChange={(e) => set({ validation_model: e.target.value })}
          className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
        />
        <input
          value={draft.default_chat_model}
          onChange={(e) => set({ default_chat_model: e.target.value })}
          className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
        />
        <input
          value={draft.fast_chat_model ?? ""}
          onChange={(e) => set({ fast_chat_model: e.target.value })}
          className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
        />
        <label className="space-y-1">
          <span className="text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
            验证超时 (ms, 1000-60000)
          </span>
          <input
            type="number"
            min={TIMEOUT_MIN_MS}
            max={TIMEOUT_MAX_MS}
            value={draft.validation_timeout_ms}
            onChange={(e) =>
              set({
                validation_timeout_ms: clampInt(
                  e.target.value,
                  TIMEOUT_MIN_MS,
                  TIMEOUT_MAX_MS,
                ),
              })
            }
            className="w-full h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
        </label>
        <input
          value={draft.proxy_name ?? ""}
          onChange={(e) => set({ proxy_name: e.target.value })}
          placeholder="proxy_name"
          className="h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
        />
        <label className="space-y-1">
          <span className="text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
            text 并发 (1-32)
          </span>
          <input
            type="number"
            min={CONCURRENCY_MIN}
            max={CONCURRENCY_MAX}
            value={draft.text_concurrency_per_key}
            onChange={(e) =>
              set({
                text_concurrency_per_key: clampInt(
                  e.target.value,
                  CONCURRENCY_MIN,
                  CONCURRENCY_MAX,
                ),
              })
            }
            className="w-full h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
        </label>
        <label className="space-y-1">
          <span className="text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
            image 并发 (1-32)
          </span>
          <input
            type="number"
            min={CONCURRENCY_MIN}
            max={CONCURRENCY_MAX}
            value={draft.image_concurrency_per_key}
            onChange={(e) =>
              set({
                image_concurrency_per_key: clampInt(
                  e.target.value,
                  CONCURRENCY_MIN,
                  CONCURRENCY_MAX,
                ),
              })
            }
            className="w-full h-10 rounded-xl border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
        </label>
      </div>
      {/* review §9 / #13: 编辑同样支持 purposes 多选 */}
      <div className="space-y-2">
        <label className="text-xs uppercase tracking-wider text-[var(--fg-2)]">
          用途 (purposes)
        </label>
        <div className="flex gap-2 flex-wrap">
          {PURPOSES.map((p) => {
            const active = draft.purposes.includes(p);
            return (
              <button
                key={p}
                type="button"
                onClick={() =>
                  set({ purposes: togglePurpose(draft.purposes, p) })
                }
                className={
                  "px-2.5 py-1 rounded-lg border text-xs transition-colors " +
                  (active
                    ? "bg-[var(--color-lumen-amber)] text-black border-[var(--color-lumen-amber)]"
                    : "bg-white/[0.03] text-neutral-300 border-white/10 hover:bg-white/[0.08]")
                }
              >
                {p}
              </button>
            );
          })}
        </div>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <label className="flex items-center justify-between rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2 text-sm">
          启用
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => set({ enabled: e.target.checked })}
          />
        </label>
        <label className="flex items-center justify-between rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2 text-sm">
          注册
          <input
            type="checkbox"
            checked={draft.public_signup_enabled}
            onChange={(e) => set({ public_signup_enabled: e.target.checked })}
          />
        </label>
        <label className="flex items-center justify-between rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2 text-sm">
          绑定
          <input
            type="checkbox"
            checked={draft.user_bind_enabled}
            onChange={(e) => set({ user_bind_enabled: e.target.checked })}
          />
        </label>
        <label className="flex items-center justify-between rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2 text-sm">
          探活 Key
          <input
            type="password"
            value={draft.probe_key}
            onChange={(e) => set({ probe_key: e.target.value })}
            className="w-24 rounded-md bg-transparent text-right text-xs outline-none"
          />
        </label>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          onClick={onSave}
          disabled={busy}
          className="inline-flex h-10 items-center gap-2 rounded-xl bg-[var(--color-lumen-amber)] px-4 text-sm font-medium text-black disabled:opacity-50"
        >
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          保存
        </button>
        <button
          type="button"
          onClick={onProbe}
          disabled={busy}
          className="inline-flex h-10 items-center gap-2 rounded-xl border border-white/10 bg-white/[0.05] px-4 text-sm disabled:opacity-50"
        >
          <TestTube2 className="w-4 h-4" />
          探活
        </button>
        {probeLabel && (
          <span className="text-xs text-neutral-500">{probeLabel}</span>
        )}
      </div>
    </section>
  );
}

function supplierToDraft(supplier: ApiSupplierTemplateOut): SupplierDraft {
  return {
    name: supplier.name,
    slug: supplier.slug,
    base_url: supplier.base_url,
    enabled: supplier.enabled,
    public_signup_enabled: supplier.public_signup_enabled,
    user_bind_enabled: supplier.user_bind_enabled,
    purposes: supplier.purposes,
    validation_model: supplier.validation_model,
    default_chat_model: supplier.default_chat_model,
    fast_chat_model: supplier.fast_chat_model,
    validation_timeout_ms: supplier.validation_timeout_ms,
    proxy_name: supplier.proxy_name ?? "",
    text_concurrency_per_key: supplier.text_concurrency_per_key,
    image_concurrency_per_key: supplier.image_concurrency_per_key,
    capabilities_jsonb: supplier.capabilities_jsonb,
    probe_key: "",
  };
}

function supplierDraftToCreateBody(draft: SupplierDraft): ApiSupplierTemplateIn {
  return {
    name: draft.name,
    slug: draft.slug,
    base_url: draft.base_url,
    enabled: draft.enabled,
    public_signup_enabled: draft.public_signup_enabled,
    user_bind_enabled: draft.user_bind_enabled,
    purposes: draft.purposes,
    validation_model: draft.validation_model,
    default_chat_model: draft.default_chat_model,
    fast_chat_model: draft.fast_chat_model,
    validation_timeout_ms: draft.validation_timeout_ms,
    proxy_name: draft.proxy_name,
    text_concurrency_per_key: draft.text_concurrency_per_key,
    image_concurrency_per_key: draft.image_concurrency_per_key,
    capabilities_jsonb: draft.capabilities_jsonb,
  };
}

function supplierDraftToPatchBody(draft: SupplierDraft): Partial<ApiSupplierTemplateIn> {
  return supplierDraftToCreateBody(draft);
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    return err.message || `请求失败 (HTTP ${err.status})`;
  }
  return err instanceof Error ? err.message : "请求失败";
}
