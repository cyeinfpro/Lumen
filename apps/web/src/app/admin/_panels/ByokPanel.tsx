"use client";

// Lumen 管理面板：BYOK（用户自带 Key）
// UI 目标：把 BYOK 开关翻成「业务模式」，新增供应商分基础/高级两段，
// 已有供应商默认折叠为 summary 行，展开后才显示编辑表单。
// 视觉风格保持 admin 既定深色主题（var(--bg-1)/(--bg-0) + amber accent）。

import { useId, useMemo, useState } from "react";
import {
  AlertCircle,
  Check,
  ChevronDown,
  Globe,
  KeyRound,
  Lock,
  Pencil,
  Plus,
  Save,
  Server,
  ShieldCheck,
  Sparkles,
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
  ByokSettingsOut,
  ByokSettingsPatchIn,
} from "@/lib/types";
import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";

type SupplierDraft = ApiSupplierTemplateIn & { probe_key: string };

const PURPOSES: Array<{ value: ByokPurpose; label: string }> = [
  { value: "chat", label: "对话" },
  { value: "image", label: "生图" },
  { value: "embedding", label: "嵌入向量" },
];

// 与后端 ApiSupplierTemplateIn 校验范围对齐（review §9 / #15）
const TIMEOUT_MIN_MS = 1000;
const TIMEOUT_MAX_MS = 60_000;
const CONCURRENCY_MIN = 1;
const CONCURRENCY_MAX = 32;
const TTL_MIN_S = 60;
const TTL_MAX_S = 3600;

const EMPTY_SUPPLIER: SupplierDraft = {
  name: "",
  slug: "",
  base_url: "",
  enabled: true,
  public_signup_enabled: true,
  user_bind_enabled: true,
  purposes: ["chat", "image"],
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

// —— 业务模式（review §9）：把 3 个开关合并为单选预设 ——

type ByokMode = "off" | "bind_only" | "key_first" | "fully_open";

type ModeToggles = Required<
  Pick<
    ByokSettingsOut,
    | "mode_enabled"
    | "byok_signup_enabled"
    | "byok_signup_bypasses_allowlist"
  >
>;

interface ModeDef {
  value: ByokMode;
  label: string;
  hint: string;
  scenario: string;
  icon: typeof Lock;
  toggles: ModeToggles;
}

const MODE_DEFS: ModeDef[] = [
  {
    value: "off",
    label: "关闭 BYOK",
    hint: "用户全部走站长配置的全局 Key，最简",
    scenario: "私有部署 / 内部演示",
    icon: Lock,
    toggles: { mode_enabled: false, byok_signup_enabled: false, byok_signup_bypasses_allowlist: false },
  },
  {
    value: "bind_only",
    label: "仅老用户绑定",
    hint: "已注册用户可在账号设置里换成自己的 Key，不开放注册",
    scenario: "小范围邀请制",
    icon: KeyRound,
    toggles: { mode_enabled: true, byok_signup_enabled: false, byok_signup_bypasses_allowlist: false },
  },
  {
    value: "key_first",
    label: "Key 优先注册",
    hint: "未登录用户可先输 Key 再注册，仍要走邀请链接",
    scenario: "邀请制 + 自助 BYOK",
    icon: Sparkles,
    toggles: { mode_enabled: true, byok_signup_enabled: true, byok_signup_bypasses_allowlist: false },
  },
  {
    value: "fully_open",
    label: "完全开放注册",
    hint: "任何人凭 Key 即可注册，不再校验邀请白名单",
    scenario: "公网公开站",
    icon: Globe,
    toggles: { mode_enabled: true, byok_signup_enabled: true, byok_signup_bypasses_allowlist: true },
  },
];

function detectMode(s: ByokSettingsOut | undefined): ByokMode | null {
  if (!s) return null;
  for (const def of MODE_DEFS) {
    if (
      def.toggles.mode_enabled === s.mode_enabled &&
      def.toggles.byok_signup_enabled === s.byok_signup_enabled &&
      def.toggles.byok_signup_bypasses_allowlist === s.byok_signup_bypasses_allowlist
    ) {
      return def.value;
    }
  }
  return null; // 自定义组合：用户在高级覆盖里手动改过
}

const ADVANCED_TOGGLES: Array<{
  key: keyof ByokSettingsPatchIn;
  label: string;
  hint: string;
  requiresMode: boolean;
}> = [
  { key: "mode_enabled", label: "BYOK 总开关", hint: "关闭后所有用户走站长 Key", requiresMode: false },
  { key: "byok_signup_enabled", label: "公开注册", hint: "未登录用户也能用 Key 注册", requiresMode: true },
  { key: "byok_signup_bypasses_allowlist", label: "绕过白名单", hint: "BYOK 注册免邀请链接 / allowlist", requiresMode: true },
];

// —— 工具 ——

function clampInt(raw: string | number, min: number, max: number): number {
  const n = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(n)) return min;
  return Math.max(min, Math.min(max, Math.floor(n)));
}

function validateBaseUrl(v: string): string | null {
  if (!v.trim()) return "必填";
  try {
    const url = new URL(v);
    if (url.protocol !== "http:" && url.protocol !== "https:") return "必须是 http(s)";
    if (url.username || url.password) return "URL 不能包含账号密码";
    return null;
  } catch {
    return "URL 格式错误";
  }
}

function safeHostname(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

function togglePurpose(purposes: ByokPurpose[], target: ByokPurpose): ByokPurpose[] {
  if (purposes.includes(target)) {
    const next = purposes.filter((p) => p !== target);
    return next.length > 0 ? next : purposes;
  }
  return [...purposes, target];
}

// —— 表单预设：常见 OpenAI / 兼容站点一键填充 ——

const SUPPLIER_PRESETS: Array<{ value: string; label: string; apply: () => SupplierDraft }> = [
  {
    value: "openai",
    label: "OpenAI 官方",
    apply: () => ({
      ...EMPTY_SUPPLIER,
      name: "OpenAI",
      slug: "openai",
      base_url: "https://api.openai.com",
      validation_model: "gpt-5.4",
      default_chat_model: "gpt-5.4",
      fast_chat_model: "gpt-5.4-mini",
      purposes: ["chat", "image"],
    }),
  },
  {
    value: "compatible",
    label: "OpenAI 兼容站点",
    apply: () => ({
      ...EMPTY_SUPPLIER,
      validation_model: "gpt-5.4",
      default_chat_model: "gpt-5.4",
      fast_chat_model: "gpt-5.4-mini",
    }),
  },
  { value: "blank", label: "自定义（清空）", apply: () => ({ ...EMPTY_SUPPLIER }) },
];

// —— 主组件 ——

export function ByokPanel() {
  const qc = useQueryClient();
  const settingsQ = useQuery({ queryKey: ["admin", "byok-settings"], queryFn: getByokSettings, retry: false });
  const suppliersQ = useQuery({ queryKey: ["admin", "byok-suppliers"], queryFn: listApiSuppliers, retry: false });

  const [settingsDraft, setSettingsDraft] = useState<ByokSettingsPatchIn>({});
  const [newSupplier, setNewSupplier] = useState<SupplierDraft>(EMPTY_SUPPLIER);
  const [newSupplierUrlError, setNewSupplierUrlError] = useState<string | null>(null);
  const [newSupplierOpen, setNewSupplierOpen] = useState(false);
  const [supplierDrafts, setSupplierDrafts] = useState<Record<string, SupplierDraft>>({});
  const [supplierUrlErrors, setSupplierUrlErrors] = useState<Record<string, string | null>>({});
  const [openSupplierId, setOpenSupplierId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [probeResult, setProbeResult] = useState<Record<string, string>>({});

  const saveSettingsMut = useMutation({
    mutationFn: () => patchByokSettings(settingsDraft),
    onSuccess: async () => {
      setSaved("系统设置已更新");
      setSettingsDraft({});
      await qc.invalidateQueries({ queryKey: ["admin", "byok-settings"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  const createMut = useMutation({
    mutationFn: () => createApiSupplier(supplierDraftToCreateBody(newSupplier)),
    onSuccess: async () => {
      setNewSupplier(EMPTY_SUPPLIER);
      setNewSupplierUrlError(null);
      setNewSupplierOpen(false);
      setSaved("供应商已创建");
      await qc.invalidateQueries({ queryKey: ["admin", "byok-suppliers"] });
    },
    onError: (err) => setError(errorText(err)),
  });

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
    mutationFn: (payload: { id: string; api_key: string }) => probeApiSupplier(payload.id, payload.api_key),
    onSuccess: (res, vars) => {
      setProbeResult((cur) => ({
        ...cur,
        [vars.id]: res.ok ? `通过 · ${res.latency_ms}ms` : `${res.error_code ?? "probe_failed"} · ${res.latency_ms}ms`,
      }));
    },
    onError: (err, vars) => {
      setProbeResult((cur) => ({ ...cur, [vars.id]: errorText(err) }));
    },
  });

  const settings = settingsQ.data;
  const suppliers = useMemo(() => suppliersQ.data?.items ?? [], [suppliersQ.data]);

  const effectiveSettings = useMemo<ByokSettingsOut | undefined>(() => {
    if (!settings) return undefined;
    return { ...settings, ...settingsDraft };
  }, [settings, settingsDraft]);
  const currentMode = detectMode(effectiveSettings);
  const totalActive = useMemo(
    () => suppliers.reduce((acc, s) => acc + s.active_credentials, 0),
    [suppliers],
  );

  const setMode = (mode: ByokMode) => {
    const def = MODE_DEFS.find((m) => m.value === mode);
    if (!def) return;
    setSettingsDraft((cur) => ({ ...cur, ...def.toggles }));
  };

  const settingsBusy = saveSettingsMut.isPending;
  const settingsDirty = Object.keys(settingsDraft).length > 0;
  const loading = settingsQ.isLoading || suppliersQ.isLoading;
  const {
    hideDays: retentionHideDays,
    deleteDays: retentionDeleteDays,
    invalid: retentionInvalid,
  } = retentionStateFor(settingsDraft, settings, effectiveSettings);

  const saveSupplier = (supplier: ApiSupplierTemplateOut) => {
    const body = supplierDrafts[supplier.id] ?? supplierToDraft(supplier);
    const urlErr = validateBaseUrl(body.base_url);
    if (urlErr) {
      setSupplierUrlErrors((current) => ({ ...current, [supplier.id]: urlErr }));
      return;
    }
    patchSupplier.mutate({ id: supplier.id, body });
  };

  const probeSupplier = (supplier: ApiSupplierTemplateOut) => {
    probeMut.mutate({
      id: supplier.id,
      api_key: (supplierDrafts[supplier.id]?.probe_key ?? "").trim(),
    });
  };

  return (
    <div className="space-y-6">
      <Overview mode={currentMode} supplierCount={suppliers.length} activeCredentials={totalActive} loading={loading} />

      <ByokSystemSettingsSection
        currentMode={currentMode}
        effectiveSettings={effectiveSettings}
        draft={settingsDraft}
        settings={settings}
        hideDays={retentionHideDays}
        deleteDays={retentionDeleteDays}
        retentionInvalid={retentionInvalid}
        busy={settingsBusy}
        dirty={settingsDirty}
        onSetMode={setMode}
        onPatch={(patch) =>
          setSettingsDraft((current) => ({ ...current, ...patch }))
        }
        onSave={() => saveSettingsMut.mutate()}
        onDiscard={() => setSettingsDraft({})}
      />

      <section className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-5 space-y-4">
        <header className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-[var(--fg-2)]">
            <Plus className="w-3.5 h-3.5" />
            新供应商
          </div>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setNewSupplierOpen((v) => !v)}
            rightIcon={<ChevronDown className={"w-3.5 h-3.5 transition-transform " + (newSupplierOpen ? "rotate-180" : "")} />}
          >
            {newSupplierOpen ? "收起" : "展开表单"}
          </Button>
        </header>

        {newSupplierOpen && (
          <div className="space-y-4">
            <div className="flex gap-2 flex-wrap">
              {SUPPLIER_PRESETS.map((preset) => (
                <Button
                  key={preset.value}
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    setNewSupplier(preset.apply());
                    setNewSupplierUrlError(null);
                  }}
                  leftIcon={<Sparkles className="w-3 h-3" />}
                >
                  {preset.label}
                </Button>
              ))}
            </div>

            <SupplierForm
              draft={newSupplier}
              urlError={newSupplierUrlError}
              onChange={setNewSupplier}
              onUrlBlur={setNewSupplierUrlError}
              showProbe={false}
            />

            <Button
              variant="primary"
              size="md"
              onClick={() => {
                const urlErr = validateBaseUrl(newSupplier.base_url);
                if (urlErr) {
                  setNewSupplierUrlError(urlErr);
                  return;
                }
                if (!newSupplier.name.trim()) {
                  setError("供应商名称不能为空");
                  return;
                }
                createMut.mutate();
              }}
              disabled={createMut.isPending}
              loading={createMut.isPending}
              leftIcon={!createMut.isPending ? <Server className="w-4 h-4" /> : undefined}
            >
              创建模板
            </Button>
          </div>
        )}
      </section>

      <ByokSupplierList
        suppliers={suppliers}
        openSupplierId={openSupplierId}
        supplierDrafts={supplierDrafts}
        supplierUrlErrors={supplierUrlErrors}
        probeResult={probeResult}
        busy={patchSupplier.isPending || probeMut.isPending}
        onToggle={(id) =>
          setOpenSupplierId((current) => (current === id ? null : id))
        }
        onChange={(id, draft) =>
          setSupplierDrafts((current) => ({ ...current, [id]: draft }))
        }
        onUrlBlur={(id, urlError) =>
          setSupplierUrlErrors((current) => ({ ...current, [id]: urlError }))
        }
        onSave={saveSupplier}
        onProbe={probeSupplier}
      />
      <ByokNotices
        error={error}
        saved={saved}
        onClearError={() => setError(null)}
        onClearSaved={() => setSaved(null)}
      />
    </div>
  );
}

function retentionStateFor(
  draft: ByokSettingsPatchIn,
  settings: ByokSettingsOut | undefined,
  effective: ByokSettingsOut | undefined,
) {
  const hideDays = draft.retention_hide_days ?? settings?.retention_hide_days ?? 3;
  const deleteDays =
    draft.retention_delete_days ?? settings?.retention_delete_days ?? 7;
  const invalid = Boolean(
    effective?.retention_hide_enabled &&
      effective?.retention_delete_enabled &&
      deleteDays < hideDays,
  );
  return { hideDays, deleteDays, invalid };
}

function ByokModeSettings({
  currentMode,
  effectiveSettings,
  onSetMode,
  onPatch,
}: {
  currentMode: ByokMode | null;
  effectiveSettings: ByokSettingsOut | undefined;
  onSetMode: (mode: ByokMode) => void;
  onPatch: (patch: ByokSettingsPatchIn) => void;
}) {
  return (
    <>
      <header className="flex items-center gap-2 text-xs uppercase tracking-wider text-[var(--fg-2)]">
        <ShieldCheck className="w-3.5 h-3.5" />
        BYOK 模式
      </header>
      <p className="text-xs text-[var(--fg-2)]">
        按业务场景一键配置；下方「高级覆盖」可手动微调 3 个原始开关。
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {MODE_DEFS.map((def) => (
          <ModeCard
            key={def.value}
            def={def}
            active={currentMode === def.value}
            onSelect={() => onSetMode(def.value)}
          />
        ))}
      </div>
      {currentMode === null && (
        <p className="flex items-start gap-2 text-xs text-[var(--color-lumen-amber)]/90">
          <AlertCircle className="mt-0.5 w-3.5 h-3.5 shrink-0" />
          当前是自定义组合（未匹配预设模式），点上方任意卡片可重置。
        </p>
      )}
      <details className="group rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-2)] overflow-hidden">
        <summary className="cursor-pointer list-none px-3 py-2 text-xs text-[var(--fg-2)] flex items-center justify-between">
          <span>高级覆盖（手动改 3 个原始开关）</span>
          <ChevronDown className="w-3.5 h-3.5 transition-transform group-open:rotate-180" />
        </summary>
        <div className="p-3 grid grid-cols-1 md:grid-cols-2 gap-3 border-t border-[var(--border-subtle)]">
          {ADVANCED_TOGGLES.map(({ key, label, hint, requiresMode }) => {
            const modeOn = Boolean(effectiveSettings?.mode_enabled);
            const disabled = requiresMode && !modeOn;
            const checked = Boolean(
              (effectiveSettings as Record<string, boolean | undefined> | undefined)?.[
                key
              ],
            );
            return (
              <ToggleRow
                key={key}
                label={label}
                hint={disabled ? "需先开启 BYOK 总开关" : hint}
                checked={checked}
                disabled={disabled}
                onChange={(value) => onPatch({ [key]: value })}
              />
            );
          })}
        </div>
      </details>
    </>
  );
}

function ByokValidationSettings({
  draft,
  settings,
  onPatch,
}: {
  draft: ByokSettingsPatchIn;
  settings: ByokSettingsOut | undefined;
  onPatch: (patch: ByokSettingsPatchIn) => void;
}) {
  return (
    <div className="space-y-2">
      <div className="text-xs uppercase tracking-wider text-[var(--fg-2)]">
        验证设置
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <FieldText
          label="验证模型"
          hint="发随机算术题给上游验证 Key（建议 gpt-5.4）"
          value={draft.validation_model ?? settings?.validation_model ?? ""}
          onChange={(value) => onPatch({ validation_model: value })}
          placeholder="gpt-5.4"
        />
        <FieldNumber
          label="验证超时 (ms)"
          hint={`单次验证 HTTP 请求超时，${TIMEOUT_MIN_MS}-${TIMEOUT_MAX_MS}（默认 15000）`}
          min={TIMEOUT_MIN_MS}
          max={TIMEOUT_MAX_MS}
          value={
            draft.validation_timeout_ms ?? settings?.validation_timeout_ms ?? 15000
          }
          onChange={(value) =>
            onPatch({
              validation_timeout_ms: clampInt(
                value,
                TIMEOUT_MIN_MS,
                TIMEOUT_MAX_MS,
              ),
            })
          }
        />
        <FieldNumber
          label="Token TTL (秒)"
          hint={`验证完到注册间的最大间隔，${TTL_MIN_S}-${TTL_MAX_S}（默认 900 = 15min）`}
          min={TTL_MIN_S}
          max={TTL_MAX_S}
          value={
            draft.pending_token_ttl_seconds ??
            settings?.pending_token_ttl_seconds ??
            900
          }
          onChange={(value) =>
            onPatch({
              pending_token_ttl_seconds: clampInt(
                value,
                TTL_MIN_S,
                TTL_MAX_S,
              ),
            })
          }
        />
      </div>
    </div>
  );
}

function ByokRetentionSettings({
  effectiveSettings,
  hideDays,
  deleteDays,
  invalid,
  onPatch,
}: {
  effectiveSettings: ByokSettingsOut | undefined;
  hideDays: number;
  deleteDays: number;
  invalid: boolean;
  onPatch: (patch: ByokSettingsPatchIn) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="text-xs uppercase tracking-wider text-[var(--fg-2)]">
        数据保留
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <ToggleRow
          label="超过窗口后用户侧隐藏"
          hint="仅影响 BYOK 用户；管理员仍可在删除前查看。"
          checked={Boolean(effectiveSettings?.retention_hide_enabled ?? true)}
          onChange={(value) => onPatch({ retention_hide_enabled: value })}
        />
        <ToggleRow
          label="自动软删除过期数据"
          hint="危险操作，默认关闭；开启后 worker 会按删除窗口软删除 BYOK 过期数据。"
          checked={Boolean(effectiveSettings?.retention_delete_enabled ?? false)}
          onChange={(value) => onPatch({ retention_delete_enabled: value })}
        />
        <FieldNumber
          label="隐藏窗口（天）"
          hint="默认 3 天；关闭隐藏开关时不生效。"
          min={1}
          max={3650}
          value={hideDays}
          onChange={(value) =>
            onPatch({ retention_hide_days: clampInt(value, 1, 3650) })
          }
        />
        <FieldNumber
          label="删除窗口（天）"
          hint="默认 7 天；关闭自动删除时不生效。"
          min={1}
          max={3650}
          value={deleteDays}
          onChange={(value) =>
            onPatch({ retention_delete_days: clampInt(value, 1, 3650) })
          }
        />
      </div>
      {invalid && (
        <p className="flex items-start gap-2 text-xs text-[var(--danger)]">
          <AlertCircle className="mt-0.5 w-3.5 h-3.5 shrink-0" />
          删除窗口不能小于隐藏窗口。
        </p>
      )}
    </div>
  );
}

function ByokSettingsActions({
  busy,
  dirty,
  retentionInvalid,
  onSave,
  onDiscard,
}: {
  busy: boolean;
  dirty: boolean;
  retentionInvalid: boolean;
  onSave: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <Button
        variant="primary"
        size="md"
        onClick={onSave}
        disabled={busy || !dirty || retentionInvalid}
        loading={busy}
        leftIcon={!busy ? <Save className="w-4 h-4" /> : undefined}
      >
        保存系统设置
      </Button>
      {dirty && (
        <Button
          variant="link"
          size="sm"
          onClick={onDiscard}
          className="text-[var(--fg-2)] no-underline hover:underline"
        >
          丢弃改动
        </Button>
      )}
    </div>
  );
}

function ByokSystemSettingsSection({
  currentMode,
  effectiveSettings,
  draft,
  settings,
  hideDays,
  deleteDays,
  retentionInvalid,
  busy,
  dirty,
  onSetMode,
  onPatch,
  onSave,
  onDiscard,
}: {
  currentMode: ByokMode | null;
  effectiveSettings: ByokSettingsOut | undefined;
  draft: ByokSettingsPatchIn;
  settings: ByokSettingsOut | undefined;
  hideDays: number;
  deleteDays: number;
  retentionInvalid: boolean;
  busy: boolean;
  dirty: boolean;
  onSetMode: (mode: ByokMode) => void;
  onPatch: (patch: ByokSettingsPatchIn) => void;
  onSave: () => void;
  onDiscard: () => void;
}) {
  return (
    <section className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-5 space-y-4">
      <ByokModeSettings
        currentMode={currentMode}
        effectiveSettings={effectiveSettings}
        onSetMode={onSetMode}
        onPatch={onPatch}
      />
      <ByokValidationSettings
        draft={draft}
        settings={settings}
        onPatch={onPatch}
      />
      <ByokRetentionSettings
        effectiveSettings={effectiveSettings}
        hideDays={hideDays}
        deleteDays={deleteDays}
        invalid={retentionInvalid}
        onPatch={onPatch}
      />
      <ByokSettingsActions
        busy={busy}
        dirty={dirty}
        retentionInvalid={retentionInvalid}
        onSave={onSave}
        onDiscard={onDiscard}
      />
    </section>
  );
}

function ByokSupplierList({
  suppliers,
  openSupplierId,
  supplierDrafts,
  supplierUrlErrors,
  probeResult,
  busy,
  onToggle,
  onChange,
  onUrlBlur,
  onSave,
  onProbe,
}: {
  suppliers: ApiSupplierTemplateOut[];
  openSupplierId: string | null;
  supplierDrafts: Record<string, SupplierDraft>;
  supplierUrlErrors: Record<string, string | null>;
  probeResult: Record<string, string>;
  busy: boolean;
  onToggle: (id: string) => void;
  onChange: (id: string, draft: SupplierDraft) => void;
  onUrlBlur: (id: string, error: string | null) => void;
  onSave: (supplier: ApiSupplierTemplateOut) => void;
  onProbe: (supplier: ApiSupplierTemplateOut) => void;
}) {
  return (
    <section className="space-y-3">
      <header className="flex items-center justify-between gap-3 px-1">
        <div className="text-xs uppercase tracking-wider text-[var(--fg-2)]">
          已有供应商 · {suppliers.length}
        </div>
      </header>
      {suppliers.length === 0 ? (
        <div className="rounded-[var(--radius-dialog)] border border-dashed border-[var(--border)] bg-[var(--bg-2)] py-10 text-center text-sm text-[var(--fg-1)]">
          还没有供应商模板，使用上方「新供应商」创建。
        </div>
      ) : (
        suppliers.map((supplier) => (
          <SupplierRow
            key={supplier.id}
            supplier={supplier}
            open={openSupplierId === supplier.id}
            onToggle={() => onToggle(supplier.id)}
            draft={supplierDrafts[supplier.id] ?? supplierToDraft(supplier)}
            urlError={supplierUrlErrors[supplier.id] ?? null}
            onChange={(draft) => onChange(supplier.id, draft)}
            onUrlBlur={(error) => onUrlBlur(supplier.id, error)}
            onSave={() => onSave(supplier)}
            onProbe={() => onProbe(supplier)}
            probeLabel={probeResult[supplier.id]}
            busy={busy}
          />
        ))
      )}
    </section>
  );
}

function ByokNotices({
  error,
  saved,
  onClearError,
  onClearSaved,
}: {
  error: string | null;
  saved: string | null;
  onClearError: () => void;
  onClearSaved: () => void;
}) {
  return (
    <>
      {error && (
        <div className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-danger">
          <AlertCircle className="mt-0.5 w-4 h-4 shrink-0" />
          <span className="flex-1">{error}</span>
          <button
            type="button"
            onClick={onClearError}
            className="type-caption text-danger/80 hover:text-danger"
          >
            {copy.action.close}
          </button>
        </div>
      )}
      {saved && (
        <div className="flex items-center gap-2 rounded-[var(--radius-card)] border border-success-border bg-success-soft px-3 py-2 type-body-sm text-success">
          <Check className="w-4 h-4" />
          <span className="flex-1">{saved}</span>
          <button
            type="button"
            onClick={onClearSaved}
            className="type-caption text-success/80 hover:text-success"
          >
            {copy.action.close}
          </button>
        </div>
      )}
    </>
  );
}

// —— 子组件 ——

function Overview({
  mode,
  supplierCount,
  activeCredentials,
  loading,
}: {
  mode: ByokMode | null;
  supplierCount: number;
  activeCredentials: number;
  loading: boolean;
}) {
  const def = mode ? MODE_DEFS.find((m) => m.value === mode) : undefined;
  const ModeIcon = def?.icon ?? AlertCircle;
  return (
    <section className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-5">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <OverviewItem icon={<ModeIcon className="w-4 h-4" />} label="当前模式" value={loading ? "加载中…" : (def?.label ?? "自定义")} />
        <OverviewItem icon={<Server className="w-4 h-4" />} label="供应商模板" value={loading ? "—" : `${supplierCount} 个`} />
        <OverviewItem icon={<KeyRound className="w-4 h-4" />} label="活跃 Key 总数" value={loading ? "—" : `${activeCredentials} 把`} />
      </div>
    </section>
  );
}

function OverviewItem({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="flex items-start gap-3">
      <span className="mt-0.5 flex h-7 w-7 items-center justify-center rounded-[var(--radius-card)] bg-[var(--bg-2)] border border-[var(--border-subtle)] text-[var(--color-lumen-amber)]">
        {icon}
      </span>
      <div className="flex flex-col">
        <span className="text-[11px] uppercase tracking-wider text-[var(--fg-2)]">{label}</span>
        <span className="text-sm text-[var(--fg-0)] mt-0.5">{value}</span>
      </div>
    </div>
  );
}

function ModeCard({ def, active, onSelect }: { def: ModeDef; active: boolean; onSelect: () => void }) {
  const Icon = def.icon;
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      className={
        "text-left rounded-[var(--radius-panel)] border p-3 transition-colors " +
        (active
          ? "border-[var(--color-lumen-amber)]/60 bg-[var(--color-lumen-amber)]/10"
          : "border-[var(--border)] bg-[var(--bg-2)] hover:bg-[var(--bg-3)]")
      }
    >
      <div className="flex items-center gap-2">
        <span
          className={
            "flex h-7 w-7 items-center justify-center rounded-[var(--radius-card)] " +
            (active ? "bg-[var(--color-lumen-amber)] text-black" : "bg-[var(--bg-2)] text-[var(--fg-1)]")
          }
        >
          <Icon className="w-3.5 h-3.5" />
        </span>
        <span className="text-sm font-medium text-[var(--fg-0)]">{def.label}</span>
      </div>
      <p className="mt-2 text-xs text-[var(--fg-2)] leading-relaxed">{def.hint}</p>
      <p className="mt-2 text-[11px] text-[var(--fg-2)]">适合：{def.scenario}</p>
    </button>
  );
}

function SupplierRow({
  supplier,
  open,
  onToggle,
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
  open: boolean;
  onToggle: () => void;
  draft: SupplierDraft;
  urlError: string | null;
  onChange: (next: SupplierDraft) => void;
  onUrlBlur: (err: string | null) => void;
  onSave: () => void;
  onProbe: () => void;
  probeLabel?: string;
  busy: boolean;
}) {
  return (
    <article className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 overflow-hidden">
      <header className="flex flex-wrap items-center gap-3 px-4 py-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm text-[var(--fg-0)] truncate">{supplier.name}</h3>
            {supplier.enabled ? (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)] text-[11px] bg-success-soft text-success border border-success-border">
                <Check className="w-3 h-3" /> 启用
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)] text-[11px] bg-[var(--bg-2)] text-[var(--fg-2)] border border-[var(--border)]">
                已禁用
              </span>
            )}
          </div>
          <p className="text-xs text-[var(--fg-2)] truncate mt-0.5">
            {safeHostname(supplier.base_url)} · {supplier.purposes.join("/")}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap text-xs">
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-2.5 py-1 text-[var(--fg-1)]">
            活跃 Key {supplier.active_credentials}
          </span>
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-2.5 py-1 text-[var(--fg-1)]">
            验证模型 {supplier.validation_model}
          </span>
          <Button
            variant="secondary"
            size="sm"
            onClick={onToggle}
            aria-expanded={open}
            leftIcon={<Pencil className="w-3.5 h-3.5" />}
          >
            {open ? "收起" : copy.action.edit}
          </Button>
        </div>
      </header>

      {Object.keys(supplier.recent_error_counts).length > 0 && (
        <p className="px-4 pb-2 text-xs text-[var(--fg-2)]">
          近期错误：
          {Object.entries(supplier.recent_error_counts).map(([k, v]) => `${k}:${v}`).join(" · ")}
        </p>
      )}

      {open && (
        <div className="border-t border-[var(--border)] p-4 space-y-4 bg-[var(--bg-2)]">
          <SupplierForm draft={draft} urlError={urlError} onChange={onChange} onUrlBlur={onUrlBlur} showProbe />
          <div className="flex items-center gap-2 flex-wrap">
            <Button
              variant="primary"
              size="md"
              onClick={onSave}
              disabled={busy}
              loading={busy}
              leftIcon={!busy ? <Save className="w-4 h-4" /> : undefined}
            >
              {copy.action.save}
            </Button>
            <Button
              variant="secondary"
              size="md"
              onClick={onProbe}
              disabled={busy || !draft.probe_key.trim()}
              leftIcon={<TestTube2 className="w-4 h-4" />}
            >
              探活
            </Button>
            {probeLabel && <span className="type-caption text-[var(--fg-2)]">{probeLabel}</span>}
          </div>
        </div>
      )}
    </article>
  );
}

function SupplierForm({
  draft,
  urlError,
  onChange,
  onUrlBlur,
  showProbe,
}: {
  draft: SupplierDraft;
  urlError: string | null;
  onChange: (next: SupplierDraft) => void;
  onUrlBlur: (err: string | null) => void;
  showProbe: boolean;
}) {
  const set = (patch: Partial<SupplierDraft>) => onChange({ ...draft, ...patch });
  return (
    <div className="space-y-4">
      <div className="space-y-3">
        <FieldText
          label="名称"
          hint="管理员后台展示的名字（如 OpenAI、SiliconFlow）"
          value={draft.name}
          onChange={(v) => set({ name: v })}
          placeholder="OpenAI"
        />
        <FieldText
          label="Base URL"
          hint="OpenAI 兼容根域名，不要带 /v1 后缀"
          value={draft.base_url}
          onChange={(v) => {
            set({ base_url: v });
            if (urlError) onUrlBlur(null);
          }}
          onBlur={(v) => onUrlBlur(validateBaseUrl(v))}
          placeholder="https://api.example.com"
          error={urlError}
        />
        <PurposesField
          purposes={draft.purposes}
          onToggle={(p) => set({ purposes: togglePurpose(draft.purposes, p) })}
        />
        <ToggleRow
          checked={draft.enabled}
          label="启用此供应商"
          hint="禁用后用户和探活均不可使用"
          onChange={(v) => set({ enabled: v })}
        />
      </div>

      <details className="group rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-2)] overflow-hidden">
        <summary className="cursor-pointer list-none px-3 py-2 text-xs text-[var(--fg-2)] flex items-center justify-between">
          <span>高级配置</span>
          <ChevronDown className="w-3.5 h-3.5 transition-transform group-open:rotate-180" />
        </summary>
        <div className="p-3 space-y-3 border-t border-[var(--border-subtle)]">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <FieldText label="Slug" hint="可选；留空后端自动从 name 生成（仅小写英文/数字）" value={draft.slug ?? ""} onChange={(v) => set({ slug: v })} placeholder="auto" />
            <FieldText label="代理名 proxy_name" hint="可选；走 admin 已配置的 proxy 池" value={draft.proxy_name ?? ""} onChange={(v) => set({ proxy_name: v })} placeholder="无" />
            <FieldText label="验证模型" hint="探活时用的 chat model" value={draft.validation_model} onChange={(v) => set({ validation_model: v })} placeholder="gpt-5.4" />
            <FieldText label="默认对话模型" hint="该供应商下用户对话的默认 model" value={draft.default_chat_model} onChange={(v) => set({ default_chat_model: v })} placeholder="gpt-5.4" />
            <FieldText label="快速对话模型" hint="标题生成 / 上下文等轻任务使用" value={draft.fast_chat_model ?? ""} onChange={(v) => set({ fast_chat_model: v })} placeholder="gpt-5.4-mini" />
            <FieldNumber
              label="验证超时 (ms)"
              hint={`${TIMEOUT_MIN_MS}-${TIMEOUT_MAX_MS}`}
              min={TIMEOUT_MIN_MS}
              max={TIMEOUT_MAX_MS}
              value={draft.validation_timeout_ms}
              onChange={(v) => set({ validation_timeout_ms: clampInt(v, TIMEOUT_MIN_MS, TIMEOUT_MAX_MS) })}
            />
            <FieldNumber
              label="text 并发 / Key"
              hint={`${CONCURRENCY_MIN}-${CONCURRENCY_MAX}`}
              min={CONCURRENCY_MIN}
              max={CONCURRENCY_MAX}
              value={draft.text_concurrency_per_key}
              onChange={(v) => set({ text_concurrency_per_key: clampInt(v, CONCURRENCY_MIN, CONCURRENCY_MAX) })}
            />
            <FieldNumber
              label="image 并发 / Key"
              hint={`${CONCURRENCY_MIN}-${CONCURRENCY_MAX}`}
              min={CONCURRENCY_MIN}
              max={CONCURRENCY_MAX}
              value={draft.image_concurrency_per_key}
              onChange={(v) => set({ image_concurrency_per_key: clampInt(v, CONCURRENCY_MIN, CONCURRENCY_MAX) })}
            />
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-2 border-t border-[var(--border-subtle)]">
            <ToggleRow
              checked={draft.public_signup_enabled}
              label="允许公开注册使用"
              hint="该供应商可被未登录用户在 BYOK 注册流程中选择"
              onChange={(v) => set({ public_signup_enabled: v })}
            />
            <ToggleRow
              checked={draft.user_bind_enabled}
              label="允许已登录用户绑定"
              hint="该供应商出现在账号设置 → API Key 列表中"
              onChange={(v) => set({ user_bind_enabled: v })}
            />
          </div>
        </div>
      </details>

      {showProbe && (
        <FieldText
          label="探活 Key"
          hint="临时填一个用户 Key，仅用本次探活，不会保存到后端"
          value={draft.probe_key}
          onChange={(v) => set({ probe_key: v })}
          placeholder="sk-..."
          isPassword
        />
      )}
    </div>
  );
}

// —— 通用 field 组件 ——

function FieldText({
  label,
  hint,
  value,
  onChange,
  onBlur,
  placeholder,
  error,
  isPassword,
}: {
  label: string;
  hint?: string;
  value: string;
  onChange: (v: string) => void;
  onBlur?: (v: string) => void;
  placeholder?: string;
  error?: string | null;
  isPassword?: boolean;
}) {
  const id = useId();
  return (
    <label htmlFor={id} className="flex flex-col gap-1">
      <span className="text-[11px] uppercase tracking-wider text-[var(--fg-1)]">{label}</span>
      <input
        id={id}
        type={isPassword ? "password" : "text"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur ? (e) => onBlur(e.target.value) : undefined}
        placeholder={placeholder}
        className={
          "h-10 rounded-[var(--radius-control)] bg-[var(--bg-0)] px-3 text-sm border focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 placeholder:text-[var(--fg-3)] transition-colors " +
          (error ? "border-danger-border" : "border-[var(--border)]")
        }
      />
      {error ? (
        <span role="alert" aria-live="assertive" className="text-[11px] text-danger">
          {error}
        </span>
      ) : hint ? (
        <span className="text-[11px] text-[var(--fg-2)]">{hint}</span>
      ) : null}
    </label>
  );
}

function FieldNumber({
  label,
  hint,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
}) {
  const id = useId();
  return (
    <label htmlFor={id} className="flex flex-col gap-1">
      <span className="text-[11px] uppercase tracking-wider text-[var(--fg-1)]">{label}</span>
      <input
        id={id}
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(clampInt(e.target.value, min, max))}
        className="h-10 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
      />
      {hint && <span className="text-[11px] text-[var(--fg-2)]">{hint}</span>}
    </label>
  );
}

function PurposesField({ purposes, onToggle }: { purposes: ByokPurpose[]; onToggle: (p: ByokPurpose) => void }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[11px] uppercase tracking-wider text-[var(--fg-1)]">用途</span>
      <div className="flex gap-2 flex-wrap">
        {PURPOSES.map((p) => {
          const active = purposes.includes(p.value);
          return (
            <button
              key={p.value}
              type="button"
              onClick={() => onToggle(p.value)}
              className={
                "px-2.5 py-1 rounded-[var(--radius-card)] border text-xs transition-colors " +
                (active
                  ? "bg-[var(--color-lumen-amber)] text-black border-[var(--color-lumen-amber)]"
                  : "bg-[var(--bg-2)] text-[var(--fg-1)] border-[var(--border)] hover:bg-[var(--bg-3)]")
              }
            >
              {p.label}
            </button>
          );
        })}
      </div>
      <span className="text-[11px] text-[var(--fg-2)]">
        该供应商支持的模型类型，影响下游路由（至少选 1 个）
      </span>
    </div>
  );
}

function ToggleRow({
  checked,
  label,
  hint,
  disabled,
  onChange,
}: {
  checked: boolean;
  label: string;
  hint?: string;
  disabled?: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label
      className={
        "flex items-start justify-between gap-3 rounded-[var(--radius-panel)] border border-[var(--border-subtle)] bg-[var(--bg-2)] px-3 py-2 " +
        (disabled ? "opacity-50" : "")
      }
    >
      <span className="flex flex-col">
        <span className="text-sm text-[var(--fg-0)]">{label}</span>
        {hint && <span className="text-[11px] text-[var(--fg-2)] mt-0.5">{hint}</span>}
      </span>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-1"
      />
    </label>
  );
}

// —— mappers ——

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
