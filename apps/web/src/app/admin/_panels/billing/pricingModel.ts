import type {
  AdminPricingBulkIn,
  AdminPricingBulkRatesIn,
  PricingRuleOut,
  PricingRuleUpsertIn,
  SystemSettingItem,
} from "@/lib/types";

export const DEFAULT_IMAGE_THRESHOLDS: Record<string, number> = {
  "1k": 1_572_864,
  "2k": 3_686_400,
  "4k": 8_294_400,
};

export const BULK_RATE_FIELDS: {
  key: keyof AdminPricingBulkRatesIn;
  label: string;
  numeric?: boolean;
}[] = [
  { key: "input", label: "输入 ¥/1K" },
  { key: "output", label: "输出 ¥/1K" },
  { key: "cache_read", label: "缓存读 ¥/1K" },
  { key: "cache_creation", label: "缓存写 ¥/1K" },
  { key: "cache_creation_5m", label: "缓存写 5m ¥/1K" },
  { key: "cache_creation_1h", label: "缓存写 1h ¥/1K" },
  { key: "image_output", label: "图片输出 ¥/1K" },
  { key: "reasoning", label: "推理 ¥/1K" },
  { key: "input_priority", label: "Priority 输入" },
  { key: "output_priority", label: "Priority 输出" },
  { key: "cache_read_priority", label: "Priority 缓存读" },
  { key: "long_context_threshold", label: "长上下文阈值", numeric: true },
  {
    key: "long_context_input_multiplier",
    label: "长上下文输入倍数",
    numeric: true,
  },
  {
    key: "long_context_output_multiplier",
    label: "长上下文输出倍数",
    numeric: true,
  },
];

export const VIDEO_PRICING_VARIANTS = [
  "t2v",
  "i2v",
  "reference_image",
  "reference_video",
  "reference",
] as const;

export type VideoPricingVariant = (typeof VIDEO_PRICING_VARIANTS)[number];

export const VIDEO_RESOLUTIONS = ["480p", "720p", "1080p", "4k"] as const;

export type VideoResolution = (typeof VIDEO_RESOLUTIONS)[number];
type VideoPriceBucket = VideoResolution | "base";
type VideoRuleMap = Partial<
  Record<VideoPricingVariant, Partial<Record<VideoPriceBucket, PricingRuleOut>>>
>;

export type VideoRuleRow = {
  model: string;
  rules: VideoRuleMap;
  updated_at?: string;
};

export type ModelRuleRow = {
  model: string;
  input?: PricingRuleOut;
  output?: PricingRuleOut;
  updated_at?: string;
};

export type ImagePricingRow = {
  tier: string;
  row?: PricingRuleOut;
  threshold: number;
};

export type BillingSettingsDraft = Partial<{
  enabled: string;
  allowNegative: string;
  showEstimate: string;
  lowBalanceRmb: string;
  rate: string;
}>;

export type BillingSettingsValues = {
  enabled: string;
  allowNegative: string;
  showEstimate: string;
  lowBalanceRmb: string;
  rate: string;
  secretConfigured: boolean;
};

export type VideoNewModelDraft = {
  model: string;
  t2v: string;
  i2v: string;
  reference_image: string;
  reference_video: string;
  reference: string;
  note: string;
};

export const EMPTY_VIDEO_NEW_MODEL: VideoNewModelDraft = {
  model: "",
  t2v: "",
  i2v: "",
  reference_image: "",
  reference_video: "",
  reference: "",
  note: "需按火山最新价格复核",
};

const VIDEO_OFFICIAL_PRICE_PRESETS: {
  model: string;
  prices: Record<VideoPricingVariant, Partial<Record<VideoResolution, number>>>;
  note: string;
}[] = [
  {
    model: "seedance-2.0",
    prices: {
      t2v: { "480p": 46, "720p": 46, "1080p": 51, "4k": 26 },
      i2v: { "480p": 46, "720p": 46, "1080p": 51, "4k": 26 },
      reference_image: { "480p": 46, "720p": 46, "1080p": 51, "4k": 26 },
      reference_video: { "480p": 28, "720p": 28, "1080p": 31, "4k": 16 },
      reference: { "480p": 46, "720p": 46, "1080p": 51, "4k": 26 },
    },
    note: "火山官方 token 单价：480/720P 无视频 46、含视频 28；1080P 无视频 51、含视频 31；4k 无视频 26、含视频 16。实际视频费用还取决于分辨率最低 token 用量。",
  },
  {
    model: "seedance-2.0-fast",
    prices: {
      t2v: { "480p": 37, "720p": 37 },
      i2v: { "480p": 37, "720p": 37 },
      reference_image: { "480p": 37, "720p": 37 },
      reference_video: { "480p": 22, "720p": 22 },
      reference: { "480p": 37, "720p": 37 },
    },
    note: "火山官方 token 单价：480/720P 无视频 37、含视频 22 元/百万 token；Fast 不支持 1080P。实际视频费用按分辨率最低 token 用量计算，480P 与 720P 不同价。",
  },
  {
    model: "seedance-2.0-mini",
    prices: {
      t2v: { "480p": 23, "720p": 23 },
      i2v: { "480p": 23, "720p": 23 },
      reference_image: { "480p": 23, "720p": 23 },
      reference_video: { "480p": 14, "720p": 14 },
      reference: { "480p": 23, "720p": 23 },
    },
    note: "火山/ModelArk 官方价折算：480/720P 无视频 23、含视频 14 元/百万 token；Mini 不支持 1080P/4k。实际视频费用按分辨率最低 token 用量计算。",
  },
];

function settingValue(
  settingsByKey: Map<string, string | null>,
  key: string,
  fallback: string,
): string {
  const value = settingsByKey.get(key);
  return value == null || value === "" ? fallback : value;
}

function microToRmb(value?: string | null): string {
  const raw = Number(value ?? 0);
  return Number.isFinite(raw) ? String(raw / 1_000_000) : "0";
}

function rmbToMicro(value: string): string {
  const raw = Number(value);
  return Number.isFinite(raw) ? String(Math.round(raw * 1_000_000)) : "0";
}

export function resolveBillingSettings(
  items: SystemSettingItem[],
  draft: BillingSettingsDraft,
): BillingSettingsValues {
  const settingsByKey = new Map(items.map((item) => [item.key, item.value]));
  const itemsByKey = new Map(items.map((item) => [item.key, item]));
  return {
    enabled:
      draft.enabled ?? settingValue(settingsByKey, "billing.enabled", "0"),
    allowNegative:
      draft.allowNegative ??
      settingValue(settingsByKey, "billing.allow_negative_balance", "0"),
    showEstimate:
      draft.showEstimate ??
      settingValue(settingsByKey, "billing.show_estimate_in_composer", "1"),
    lowBalanceRmb:
      draft.lowBalanceRmb ??
      microToRmb(
        settingValue(
          settingsByKey,
          "billing.low_balance_warn_micro",
          "2000000",
        ),
      ),
    rate:
      draft.rate ??
      settingValue(settingsByKey, "billing.usd_to_rmb_rate", "1.0"),
    secretConfigured:
      itemsByKey.get("billing.redemption_code_secret")?.has_value ?? false,
  };
}

export function billingSettingsPayload(values: BillingSettingsValues) {
  return [
    { key: "billing.enabled", value: values.enabled },
    {
      key: "billing.allow_negative_balance",
      value: values.allowNegative,
    },
    {
      key: "billing.show_estimate_in_composer",
      value: values.showEstimate,
    },
    {
      key: "billing.low_balance_warn_micro",
      value: rmbToMicro(values.lowBalanceRmb),
    },
    { key: "billing.usd_to_rmb_rate", value: values.rate },
  ];
}

export function buildImagePricingRows(
  items: PricingRuleOut[],
  savedThresholds: Record<string, number>,
  customTiers: string[],
): ImagePricingRow[] {
  const tiers = new Set(Object.keys(DEFAULT_IMAGE_THRESHOLDS));
  const rowsByTier = new Map<string, PricingRuleOut>();
  for (const tier of Object.keys(savedThresholds)) tiers.add(tier);
  for (const tier of customTiers) tiers.add(tier);
  for (const item of items) {
    if (item.scope !== "image_size" || item.unit !== "per_image") continue;
    tiers.add(item.key);
    rowsByTier.set(item.key, item);
  }
  return Array.from(tiers)
    .sort(
      (left, right) =>
        (savedThresholds[left] ?? 0) - (savedThresholds[right] ?? 0) ||
        left.localeCompare(right),
    )
    .map((tier) => ({
      tier,
      row: rowsByTier.get(tier),
      threshold: savedThresholds[tier] ?? 0,
    }));
}

export function buildImagePricingRequest(
  rows: ImagePricingRow[],
  imagePrices: Record<string, string>,
  imageThresholds: Record<string, string>,
): {
  items: PricingRuleUpsertIn[];
  imageSizeThresholds: Record<string, number>;
} {
  const items = rows.map(({ tier, row }) => ({
    scope: "image_size" as const,
    key: tier,
    variant: "default",
    unit: "per_image" as const,
    price_rmb: imagePrices[tier] ?? row?.price.rmb ?? "0",
    enabled: row?.enabled ?? true,
    note: row?.note ?? "",
  }));
  const imageSizeThresholds = Object.fromEntries(
    rows.map(({ tier, threshold }) => [
      tier,
      Number(imageThresholds[tier] ?? threshold) || 0,
    ]),
  );
  return { items, imageSizeThresholds };
}

export function groupModelRules(items: PricingRuleOut[]): ModelRuleRow[] {
  const map = new Map<string, ModelRuleRow>();
  for (const item of items) {
    if (item.scope !== "chat_model") continue;
    const row = map.get(item.key) ?? { model: item.key };
    if (item.unit === "per_1k_tokens_in") row.input = item;
    if (item.unit === "per_1k_tokens_out") row.output = item;
    row.updated_at = [row.updated_at, item.updated_at]
      .filter(Boolean)
      .sort()
      .at(-1);
    map.set(item.key, row);
  }
  return Array.from(map.values()).sort((left, right) =>
    left.model.localeCompare(right.model),
  );
}

function modelRuleUpsert(
  model: string,
  rule: PricingRuleOut,
  price: string,
  enabled: boolean,
): PricingRuleUpsertIn {
  return {
    scope: "chat_model",
    key: model,
    variant: rule.variant,
    unit: rule.unit,
    price_rmb: price,
    priority: rule.priority,
    enabled,
    note: rule.note,
  };
}

export function buildModelPricingItems(
  rows: ModelRuleRow[],
  drafts: Record<string, string>,
): PricingRuleUpsertIn[] {
  const items: PricingRuleUpsertIn[] = [];
  for (const row of rows) {
    if (row.input) {
      items.push(
        modelRuleUpsert(
          row.model,
          row.input,
          drafts[`${row.model}:in`] ?? row.input.price.rmb,
          row.input.enabled,
        ),
      );
    }
    if (row.output) {
      items.push(
        modelRuleUpsert(
          row.model,
          row.output,
          drafts[`${row.model}:out`] ?? row.output.price.rmb,
          row.output.enabled,
        ),
      );
    }
  }
  return items;
}

export function buildDisabledModelPricingItems(
  row: ModelRuleRow,
): PricingRuleUpsertIn[] {
  const items: PricingRuleUpsertIn[] = [];
  if (row.input) {
    items.push(
      modelRuleUpsert(row.model, row.input, row.input.price.rmb, false),
    );
  }
  if (row.output) {
    items.push(
      modelRuleUpsert(row.model, row.output, row.output.price.rmb, false),
    );
  }
  return items;
}

function optionalRate(
  rates: Record<string, string>,
  key: string,
): string | undefined {
  const value = rates[key]?.trim();
  return value || undefined;
}

function optionalNumber(
  rates: Record<string, string>,
  key: string,
): number | undefined {
  const raw = rates[key]?.trim();
  if (!raw) return undefined;
  const value = Number(raw);
  return Number.isFinite(value) ? value : undefined;
}

export function buildBulkPricingInput({
  model,
  channel,
  priority,
  rates,
}: {
  model: string;
  channel: string;
  priority: string;
  rates: Record<string, string>;
}): AdminPricingBulkIn {
  return {
    model: model.trim(),
    channel: channel.trim() || null,
    priority: Number.parseInt(priority, 10) || 0,
    rates: {
      input: optionalRate(rates, "input"),
      output: optionalRate(rates, "output"),
      cache_read: optionalRate(rates, "cache_read"),
      cache_creation: optionalRate(rates, "cache_creation"),
      cache_creation_5m: optionalRate(rates, "cache_creation_5m"),
      cache_creation_1h: optionalRate(rates, "cache_creation_1h"),
      image_output: optionalRate(rates, "image_output"),
      reasoning: optionalRate(rates, "reasoning"),
      input_priority: optionalRate(rates, "input_priority"),
      output_priority: optionalRate(rates, "output_priority"),
      cache_read_priority: optionalRate(rates, "cache_read_priority"),
      long_context_threshold: optionalNumber(rates, "long_context_threshold"),
      long_context_input_multiplier: optionalNumber(
        rates,
        "long_context_input_multiplier",
      ),
      long_context_output_multiplier: optionalNumber(
        rates,
        "long_context_output_multiplier",
      ),
    },
  };
}

function videoRuleVariant(
  variant: VideoPricingVariant,
  resolution: VideoResolution,
): string {
  return `${variant}_${resolution}`;
}

export function videoDraftKey(
  model: string,
  variant: VideoPricingVariant,
  resolution: VideoResolution,
): string {
  return `${model}:${videoRuleVariant(variant, resolution)}`;
}

export function videoRuleLabel(variant: VideoPricingVariant): string {
  if (variant === "t2v") return "T2V";
  if (variant === "i2v") return "I2V";
  if (variant === "reference_image") return "参考图片";
  if (variant === "reference_video") return "参考视频";
  return "Reference fallback";
}

function videoDefaultNote(variant: VideoPricingVariant): string {
  return variant === "reference"
    ? "旧 Reference fallback；仅作兼容兜底"
    : "需按火山最新价格复核";
}

function parseVideoPricingRuleVariant(
  raw: string,
): { variant: VideoPricingVariant; bucket: VideoPriceBucket } | null {
  for (const resolution of VIDEO_RESOLUTIONS) {
    const suffix = `_${resolution}`;
    if (!raw.endsWith(suffix)) continue;
    const variant = raw.slice(0, -suffix.length);
    if (VIDEO_PRICING_VARIANTS.includes(variant as VideoPricingVariant)) {
      return { variant: variant as VideoPricingVariant, bucket: resolution };
    }
    return null;
  }
  if (VIDEO_PRICING_VARIANTS.includes(raw as VideoPricingVariant)) {
    return { variant: raw as VideoPricingVariant, bucket: "base" };
  }
  return null;
}

export function groupVideoRules(items: PricingRuleOut[]): VideoRuleRow[] {
  const map = new Map<string, VideoRuleRow>();
  for (const item of items) {
    if (item.scope !== "video" || item.unit !== "per_mtoken") continue;
    const parsed = parseVideoPricingRuleVariant(item.variant);
    if (parsed == null) continue;
    const row = map.get(item.key) ?? { model: item.key, rules: {} };
    const variantRules = row.rules[parsed.variant] ?? {};
    variantRules[parsed.bucket] = item;
    row.rules[parsed.variant] = variantRules;
    row.updated_at = [row.updated_at, item.updated_at]
      .filter(Boolean)
      .sort()
      .at(-1);
    map.set(item.key, row);
  }
  return Array.from(map.values()).sort((left, right) =>
    left.model.localeCompare(right.model),
  );
}

export function videoRuleAt(
  row: VideoRuleRow,
  variant: VideoPricingVariant,
  resolution: VideoResolution,
): PricingRuleOut | undefined {
  return row.rules[variant]?.[resolution] ?? row.rules[variant]?.base;
}

export function videoRowEnabled(row: VideoRuleRow): boolean {
  return VIDEO_PRICING_VARIANTS.some((variant) =>
    Object.values(row.rules[variant] ?? {}).some((rule) => rule?.enabled),
  );
}

export function videoRowResolutionEnabled(
  row: VideoRuleRow,
  resolution: VideoResolution,
): boolean {
  return VIDEO_PRICING_VARIANTS.some(
    (variant) => videoRuleAt(row, variant, resolution)?.enabled,
  );
}

export function videoRowResolutionUpdatedAt(
  row: VideoRuleRow,
  resolution: VideoResolution,
): string | undefined {
  const dates = VIDEO_PRICING_VARIANTS.flatMap((variant) => {
    const rule = row.rules[variant]?.[resolution];
    return rule?.updated_at ? [rule.updated_at] : [];
  });
  return dates.sort().at(-1) ?? row.updated_at;
}

function formatVideoPrice(value: number): string {
  const rounded = Math.round((value + Number.EPSILON) * 10_000) / 10_000;
  return rounded
    .toFixed(4)
    .replace(/0+$/, "")
    .replace(/\.$/, "");
}

export function buildOfficialVideoDrafts(
  multiplier: number,
): Record<string, string> {
  const drafts: Record<string, string> = {};
  for (const preset of VIDEO_OFFICIAL_PRICE_PRESETS) {
    for (const variant of VIDEO_PRICING_VARIANTS) {
      for (const resolution of VIDEO_RESOLUTIONS) {
        const price = preset.prices[variant][resolution];
        if (price == null) continue;
        drafts[videoDraftKey(preset.model, variant, resolution)] =
          formatVideoPrice(price * multiplier);
      }
    }
  }
  return drafts;
}

function createVideoPricingItem({
  model,
  variant,
  resolution,
  price,
  rule,
  fallback,
  note,
}: {
  model: string;
  variant: VideoPricingVariant;
  resolution: VideoResolution;
  price?: string | null;
  rule?: PricingRuleOut;
  fallback?: PricingRuleOut;
  note?: string;
}): PricingRuleUpsertIn | null {
  const cleanPrice = (price ?? "").trim();
  if (!cleanPrice) return null;
  return {
    scope: "video",
    key: model,
    variant: videoRuleVariant(variant, resolution),
    unit: "per_mtoken",
    price_rmb: cleanPrice,
    enabled: rule?.enabled ?? fallback?.enabled ?? true,
    note: rule?.note ?? fallback?.note ?? note ?? videoDefaultNote(variant),
  };
}

function appendVideoPricingItem(
  items: PricingRuleUpsertIn[],
  input: Parameters<typeof createVideoPricingItem>[0],
) {
  const item = createVideoPricingItem(input);
  if (item) items.push(item);
}

function existingVideoPricingItems(
  rows: VideoRuleRow[],
  drafts: Record<string, string>,
): PricingRuleUpsertIn[] {
  const items: PricingRuleUpsertIn[] = [];
  for (const row of rows) {
    for (const variant of VIDEO_PRICING_VARIANTS) {
      const fallback = row.rules[variant]?.base;
      for (const resolution of VIDEO_RESOLUTIONS) {
        const rule = row.rules[variant]?.[resolution];
        appendVideoPricingItem(items, {
          model: row.model,
          variant,
          resolution,
          price:
            drafts[videoDraftKey(row.model, variant, resolution)] ??
            rule?.price.rmb ??
            fallback?.price.rmb,
          rule,
          fallback,
        });
      }
    }
  }
  return items;
}

function newVideoModelPricingItems(
  draft: VideoNewModelDraft,
): PricingRuleUpsertIn[] {
  const model = draft.model.trim();
  if (!model) return [];
  const items: PricingRuleUpsertIn[] = [];
  for (const variant of VIDEO_PRICING_VARIANTS) {
    const price = draft[variant].trim();
    if (!price) continue;
    for (const resolution of VIDEO_RESOLUTIONS) {
      appendVideoPricingItem(items, {
        model,
        variant,
        resolution,
        price,
        note: draft.note.trim() || videoDefaultNote(variant),
      });
    }
  }
  return items;
}

function officialVideoPricingItems(
  rowModels: Set<string>,
  drafts: Record<string, string>,
): PricingRuleUpsertIn[] {
  const items: PricingRuleUpsertIn[] = [];
  for (const preset of VIDEO_OFFICIAL_PRICE_PRESETS) {
    if (rowModels.has(preset.model)) continue;
    for (const variant of VIDEO_PRICING_VARIANTS) {
      for (const resolution of VIDEO_RESOLUTIONS) {
        const price = drafts[videoDraftKey(preset.model, variant, resolution)];
        if (price == null) continue;
        appendVideoPricingItem(items, {
          model: preset.model,
          variant,
          resolution,
          price,
          note: variant === "reference" ? videoDefaultNote(variant) : preset.note,
        });
      }
    }
  }
  return items;
}

export function buildVideoPricingItems({
  rows,
  drafts,
  newModel,
}: {
  rows: VideoRuleRow[];
  drafts: Record<string, string>;
  newModel: VideoNewModelDraft;
}): PricingRuleUpsertIn[] {
  const rowModels = new Set(rows.map((row) => row.model));
  return [
    ...existingVideoPricingItems(rows, drafts),
    ...newVideoModelPricingItems(newModel),
    ...officialVideoPricingItems(rowModels, drafts),
  ];
}

export function buildDisabledVideoPricingItems(
  row: VideoRuleRow,
): PricingRuleUpsertIn[] {
  const items: PricingRuleUpsertIn[] = [];
  for (const variant of VIDEO_PRICING_VARIANTS) {
    for (const [bucket, rule] of Object.entries(row.rules[variant] ?? {})) {
      if (!rule) continue;
      items.push({
        scope: "video",
        key: row.model,
        variant:
          bucket === "base"
            ? variant
            : videoRuleVariant(variant, bucket as VideoResolution),
        unit: "per_mtoken",
        price_rmb: rule.price.rmb,
        enabled: false,
        note: rule.note,
      });
    }
  }
  return items;
}
