import type {
  ApiSupplierTemplateIn,
  ApiSupplierTemplateOut,
  ByokPurpose,
  ByokSettingsOut,
  ByokSettingsPatchIn,
} from "@/lib/types";

export type SupplierDraft = ApiSupplierTemplateIn & { probe_key: string };

export const PURPOSES: Array<{ value: ByokPurpose; label: string }> = [
  { value: "chat", label: "对话" },
  { value: "image", label: "生图" },
  { value: "embedding", label: "嵌入向量" },
];

// Keep these aligned with the backend ApiSupplierTemplateIn validation range.
export const TIMEOUT_MIN_MS = 1000;
export const TIMEOUT_MAX_MS = 60_000;
export const CONCURRENCY_MIN = 1;
export const CONCURRENCY_MAX = 32;
export const TTL_MIN_S = 60;
export const TTL_MAX_S = 3600;

export const EMPTY_SUPPLIER: SupplierDraft = {
  name: "",
  slug: "",
  base_url: "",
  enabled: true,
  public_signup_enabled: true,
  user_bind_enabled: true,
  purposes: ["chat", "image"],
  validation_model: "gpt-5.4",
  default_chat_model: "gpt-5.6-sol",
  fast_chat_model: "gpt-5.4-mini",
  validation_timeout_ms: 15000,
  proxy_name: "",
  text_concurrency_per_key: 4,
  image_concurrency_per_key: 1,
  capabilities_jsonb: {},
  probe_key: "",
};

export type ByokMode = "off" | "bind_only" | "key_first" | "fully_open";

type ModeToggles = Required<
  Pick<
    ByokSettingsOut,
    | "mode_enabled"
    | "byok_signup_enabled"
    | "byok_signup_bypasses_allowlist"
  >
>;

export interface ModeDef {
  value: ByokMode;
  label: string;
  hint: string;
  scenario: string;
  toggles: ModeToggles;
}

export const MODE_DEFS: ModeDef[] = [
  {
    value: "off",
    label: "关闭 BYOK",
    hint: "用户全部走站长配置的全局 Key，最简",
    scenario: "私有部署 / 内部演示",
    toggles: {
      mode_enabled: false,
      byok_signup_enabled: false,
      byok_signup_bypasses_allowlist: false,
    },
  },
  {
    value: "bind_only",
    label: "仅老用户绑定",
    hint: "已注册用户可在账号设置里换成自己的 Key，不开放注册",
    scenario: "小范围邀请制",
    toggles: {
      mode_enabled: true,
      byok_signup_enabled: false,
      byok_signup_bypasses_allowlist: false,
    },
  },
  {
    value: "key_first",
    label: "Key 优先注册",
    hint: "未登录用户可先输 Key 再注册，仍要走邀请链接",
    scenario: "邀请制 + 自助 BYOK",
    toggles: {
      mode_enabled: true,
      byok_signup_enabled: true,
      byok_signup_bypasses_allowlist: false,
    },
  },
  {
    value: "fully_open",
    label: "完全开放注册",
    hint: "任何人凭 Key 即可注册，不再校验邀请白名单",
    scenario: "公网公开站",
    toggles: {
      mode_enabled: true,
      byok_signup_enabled: true,
      byok_signup_bypasses_allowlist: true,
    },
  },
];

export const ADVANCED_TOGGLES: Array<{
  key: keyof ByokSettingsPatchIn;
  label: string;
  hint: string;
  requiresMode: boolean;
}> = [
  {
    key: "mode_enabled",
    label: "BYOK 总开关",
    hint: "关闭后所有用户走站长 Key",
    requiresMode: false,
  },
  {
    key: "byok_signup_enabled",
    label: "公开注册",
    hint: "未登录用户也能用 Key 注册",
    requiresMode: true,
  },
  {
    key: "byok_signup_bypasses_allowlist",
    label: "绕过白名单",
    hint: "BYOK 注册免邀请链接 / allowlist",
    requiresMode: true,
  },
];

export const SUPPLIER_PRESETS: Array<{
  value: string;
  label: string;
  apply: () => SupplierDraft;
}> = [
  {
    value: "openai",
    label: "OpenAI 官方",
    apply: () => ({
      ...EMPTY_SUPPLIER,
      name: "OpenAI",
      slug: "openai",
      base_url: "https://api.openai.com",
      validation_model: "gpt-5.4",
      default_chat_model: "gpt-5.6-sol",
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
      default_chat_model: "gpt-5.6-sol",
      fast_chat_model: "gpt-5.4-mini",
    }),
  },
  {
    value: "blank",
    label: "自定义（清空）",
    apply: () => ({ ...EMPTY_SUPPLIER }),
  },
];

export function detectMode(
  settings: ByokSettingsOut | undefined,
): ByokMode | null {
  if (!settings) return null;
  for (const def of MODE_DEFS) {
    if (
      def.toggles.mode_enabled === settings.mode_enabled &&
      def.toggles.byok_signup_enabled === settings.byok_signup_enabled &&
      def.toggles.byok_signup_bypasses_allowlist ===
        settings.byok_signup_bypasses_allowlist
    ) {
      return def.value;
    }
  }
  return null;
}

export function retentionStateFor(
  draft: ByokSettingsPatchIn,
  settings: ByokSettingsOut | undefined,
  effective: ByokSettingsOut | undefined,
) {
  const hideDays =
    draft.retention_hide_days ?? settings?.retention_hide_days ?? 3;
  const deleteDays =
    draft.retention_delete_days ?? settings?.retention_delete_days ?? 7;
  const invalid = Boolean(
    effective?.retention_hide_enabled &&
      effective?.retention_delete_enabled &&
      deleteDays < hideDays,
  );
  return { hideDays, deleteDays, invalid };
}

export function clampInt(
  raw: string | number,
  min: number,
  max: number,
): number {
  const value = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, Math.floor(value)));
}

export function validateBaseUrl(value: string): string | null {
  if (!value.trim()) return "必填";
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return "必须是 http(s)";
    }
    if (url.username || url.password) return "URL 不能包含账号密码";
    return null;
  } catch {
    return "URL 格式错误";
  }
}

export function safeHostname(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

export function togglePurpose(
  purposes: ByokPurpose[],
  target: ByokPurpose,
): ByokPurpose[] {
  if (purposes.includes(target)) {
    const next = purposes.filter((purpose) => purpose !== target);
    return next.length > 0 ? next : purposes;
  }
  return [...purposes, target];
}

export function supplierToDraft(
  supplier: ApiSupplierTemplateOut,
): SupplierDraft {
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

export function supplierDraftToCreateBody(
  draft: SupplierDraft,
): ApiSupplierTemplateIn {
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

export function supplierDraftToPatchBody(
  draft: SupplierDraft,
): Partial<ApiSupplierTemplateIn> {
  return supplierDraftToCreateBody(draft);
}
