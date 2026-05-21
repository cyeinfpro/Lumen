"use client";

// Lumen 管理面板：系统设置。
// UI 目标：把工程 key 翻译成可理解的任务语言，同时保留 key 作为排错辅助信息。

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  AlertCircle,
  Bot,
  BrainCircuit,
  ChevronDown,
  ChevronRight,
  Check,
  Database,
  Gauge,
  Globe,
  ImageIcon,
  Info,
  Loader2,
  Rocket,
  RotateCcw,
  Save,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Timer,
  Zap,
  type LucideIcon,
} from "lucide-react";

import {
  useAdminModelsQuery,
  useAdminProxiesQuery,
  useProvidersQuery,
  useSystemSettingsQuery,
  useUpdateSystemSettingsMutation,
} from "@/lib/queries";
import {
  ApiError,
  getAdminContextHealth,
} from "@/lib/apiClient";
import type { SystemSettingItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { ErrorBlock } from "../page";

type Op = { kind: "set"; value: string } | { kind: "clear" };
type SettingGroupId =
  | "site"
  | "image"
  | "upstream"
  | "providers"
  | "library"
  | "update"
  | "context_auto"
  | "context_caption"
  | "context_manual"
  | "advanced";
type FilterId = "all" | SettingGroupId;
type ValueKind =
  | "integer"
  | "decimal"
  | "text"
  | "url"
  | "toggle"
  | "enum"
  | "model";

type SettingChoice = {
  value: string;
  label: string;
  description: string;
  badge?: string;
};

type SettingMeta = {
  group: SettingGroupId;
  title: string;
  summary: string;
  detail?: string;
  kind: ValueKind;
  icon: LucideIcon;
  unit?: string;
  min?: number;
  max?: number;
  step?: number;
  defaultValue?: string;
  recommended?: string;
  warning?: string;
  choices?: readonly SettingChoice[];
  keywords?: string[];
};

type DependencyState = {
  imageChannel: string;
  compressionEnabled: boolean;
  imageCaptionEnabled: boolean;
};

type ModelsQueryState = {
  isLoading: boolean;
  isError: boolean;
  errorMessage?: string;
  models: string[];
};

type ProviderStatus = {
  total: number;
  jobs: number;
  label: string;
  compact: string;
};

type UpdateProxyOption = {
  name: string;
  enabled: boolean;
  in_cooldown: boolean;
  last_latency_ms: number | null;
};

const UPDATE_USE_PROXY_POOL_KEY = "update.use_proxy_pool";
const UPDATE_PROXY_NAME_KEY = "update.proxy_name";
const MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY = "model_library.sync_use_proxy_pool";
const MODEL_LIBRARY_SYNC_PROXY_NAME_KEY = "model_library.sync_proxy_name";
const GENERATION_FAST_DEFAULT_KEY = "generation.fast_default";
const IMAGE_ENGINE_KEY = "image.engine";
const IMAGE_CHANNEL_KEY = "image.channel";
const IMAGE_GENERATION_CONCURRENCY_KEY = "image.generation_concurrency";
const IMAGE_OUTPUT_FORMAT_KEY = "image.output_format";
const IMAGE_JOB_BASE_URL_KEY = "image.job_base_url";
const SITE_PUBLIC_BASE_URL_KEY = "site.public_base_url";
const SITE_SHARE_EXPIRATION_DAYS_KEY = "site.share_expiration_days";
const HIDDEN_KEYS = new Set<string>([
  "providers",
  "image.primary_route",
  "image.text_to_image_primary_route",
  UPDATE_USE_PROXY_POOL_KEY,
  UPDATE_PROXY_NAME_KEY,
]);

const IMAGE_ENGINE_OPTIONS: readonly SettingChoice[] = [
  {
    value: "responses",
    label: "原生通道",
    description: "默认路径。走平台原生生图链路，适合日常文生图和图生图。",
  },
  {
    value: "image2",
    label: "直连通道",
    description: "直接调用图像接口，简单任务更快；4K 图生图失败会自动回到稳定路径。",
  },
  {
    value: "dual_race",
    label: "双路竞速",
    description: "原生通道和直连通道同时跑，先完成的结果返回。速度更激进，但会消耗双倍配额。",
    badge: "配额翻倍",
  },
];

const IMAGE_CHANNEL_OPTIONS: readonly SettingChoice[] = [
  {
    value: "auto",
    label: "自动混合",
    description: "按选中的供应商能力分发：支持异步任务走任务通道，不支持则走流式。",
  },
  {
    value: "stream_only",
    label: "强制流式",
    description: "所有供应商都走流式直连，不使用异步任务服务。",
  },
  {
    value: "image_jobs_only",
    label: "强制异步",
    description: "只允许支持异步任务的供应商；选中不支持的供应商会直接返回 503。",
    badge: "严格",
  },
];

const IMAGE_OUTPUT_FORMAT_OPTIONS: readonly SettingChoice[] = [
  {
    value: "jpeg",
    label: "JPG 格式",
    description: "默认选项。文件小，适合分享。",
  },
  {
    value: "png",
    label: "PNG 格式",
    description: "文件更大，适合保存透明背景或继续编辑。",
  },
];

const SETTING_META: Record<string, SettingMeta> = {
  [SITE_PUBLIC_BASE_URL_KEY]: {
    group: "site",
    title: "站点域名",
    summary: "生成邀请链接和分享链接时使用的对外访问地址。",
    detail: "公开访问域名，含 https://",
    kind: "url",
    icon: Globe,
    recommended: "生产环境建议显式填写真实 HTTPS 域名。",
    keywords: ["site", "public", "base", "url", "domain", "域名", "邀请链接", "分享链接"],
  },
  [SITE_SHARE_EXPIRATION_DAYS_KEY]: {
    group: "site",
    title: "分享链接有效期",
    summary: "新生成图片分享链接默认多久后失效。",
    detail: "0 表示永久",
    kind: "integer",
    icon: Timer,
    unit: "天",
    min: 0,
    max: 3650,
    defaultValue: "0",
    recommended: "公开分享建议设置 7 到 30 天；0 表示永久。",
    keywords: ["share", "expiration", "expires", "days", "分享", "有效期", "过期"],
  },
  "image.engine": {
    group: "image",
    title: "生图引擎",
    summary: "决定图片生成使用原生通道、直连通道还是双路竞速。",
    detail: "渲染后端",
    kind: "enum",
    icon: ImageIcon,
    defaultValue: "responses",
    recommended: "默认：原生通道",
    choices: IMAGE_ENGINE_OPTIONS,
    keywords: ["image", "engine", "responses", "image2", "dual"],
  },
  "image.channel": {
    group: "image",
    title: "异步通道",
    summary: "控制是否把支持异步任务的供应商分发到任务通道。",
    detail: "通道策略",
    kind: "enum",
    icon: Activity,
    defaultValue: "auto",
    recommended: "默认：自动混合",
    choices: IMAGE_CHANNEL_OPTIONS,
    keywords: ["image", "channel", "image_jobs", "stream", "auto", "异步"],
  },
  [IMAGE_GENERATION_CONCURRENCY_KEY]: {
    group: "image",
    title: "图片队列总并发",
    summary: "控制最多同时进入真实上游生成的图片任务数量。",
    detail: "所有尺寸共用的 FIFO 并发",
    kind: "integer",
    icon: Activity,
    min: 1,
    max: 32,
    defaultValue: "4",
    recommended: "小团队通常 4 到 8；调高前先确认 provider/key 并发和上游限额。",
    keywords: ["image", "generation", "concurrency", "queue", "图片", "队列", "并发"],
  },
  [IMAGE_OUTPUT_FORMAT_KEY]: {
    group: "image",
    title: "输出格式",
    summary: "设置新生成图片默认使用 JPG 格式还是 PNG 格式。",
    detail: "默认输出格式（透明仍走 PNG）",
    kind: "enum",
    icon: ImageIcon,
    defaultValue: "jpeg",
    recommended: "体积优先选 JPEG；需要透明或后期编辑选 PNG。",
    choices: IMAGE_OUTPUT_FORMAT_OPTIONS,
    keywords: ["image", "format", "output", "jpeg", "png", "格式", "画质"],
  },
  "image.job_base_url": {
    group: "image",
    title: "异步任务服务",
    summary: "sub2api 图片异步任务服务地址，仅在异步通道启用时使用。",
    detail: "服务根地址或 /v1 地址",
    kind: "text",
    icon: ImageIcon,
    defaultValue: "https://image-job.example.com",
    recommended: "使用默认地址，除非部署了自己的 image-job 服务。",
    keywords: ["image", "job", "image_jobs", "sub2api", "异步任务"],
  },
  "upstream.pixel_budget": {
    group: "image",
    title: "自动尺寸像素上限",
    summary: "只影响“自动尺寸”时系统怎么推导图片大小。",
    detail: "仅影响自动尺寸",
    kind: "integer",
    icon: Gauge,
    unit: "像素",
    min: 65536,
    max: 16777216,
    defaultValue: "1572864",
    recommended: "一般不用改，想让自动尺寸更大时再调高。",
    keywords: ["pixel", "budget", "size", "auto", "尺寸"],
  },
  "upstream.default_model": {
    group: "upstream",
    title: "默认对话模型",
    summary: "用户没有指定模型时，默认使用这个模型 ID。",
    kind: "model",
    icon: Bot,
    defaultValue: "gpt-5.5",
    recommended: "建议填写稳定可用的主模型。",
    keywords: ["model", "default", "模型"],
  },
  [GENERATION_FAST_DEFAULT_KEY]: {
    group: "upstream",
    title: "快速模式默认开启",
    summary: "控制全站新对话和新生图的快速模式初始状态。",
    detail: "全站默认值，用户可临时切换",
    kind: "toggle",
    icon: Zap,
    defaultValue: "1",
    recommended: "这是管理员设定的全站默认值，不是个人偏好。",
    keywords: ["fast", "default", "chat", "image", "默认", "快速"],
  },
  "upstream.global_concurrency": {
    group: "upstream",
    title: "同时请求上游的数量",
    summary: "控制全站最多同时向上游发多少个请求。",
    detail: "并发数（4–8 通常够用）",
    kind: "integer",
    icon: Activity,
    min: 1,
    max: 100,
    defaultValue: "4",
    recommended: "个人或小团队通常 4 到 8 就够。",
    keywords: ["concurrency", "并发", "上游"],
  },
  "upstream.connect_timeout_s": {
    group: "upstream",
    title: "连接等待时间",
    summary: "建立上游连接最多等多久。",
    detail: "上游连接超时（秒）",
    kind: "decimal",
    icon: Timer,
    unit: "秒",
    min: 1,
    max: 60,
    step: 0.5,
    defaultValue: "10",
    recommended: "默认：10 秒",
    keywords: ["connect", "timeout", "连接"],
  },
  "upstream.read_timeout_s": {
    group: "upstream",
    title: "生成结果等待时间",
    summary: "请求发出后，等待上游返回结果的最长时间。",
    detail: "上游读超时（秒）",
    kind: "decimal",
    icon: Timer,
    unit: "秒",
    min: 5,
    max: 1800,
    step: 1,
    defaultValue: "660",
    recommended: "默认：660 秒；4K 任务多时保持这个值。",
    keywords: ["read", "timeout", "生成", "等待"],
  },
  "upstream.write_timeout_s": {
    group: "upstream",
    title: "上传请求等待时间",
    summary: "上传图片或较大请求体时，最多等多久。",
    detail: "上传写超时（秒）",
    kind: "decimal",
    icon: Timer,
    unit: "秒",
    min: 1,
    max: 120,
    step: 1,
    defaultValue: "30",
    recommended: "默认：30 秒",
    keywords: ["write", "timeout", "上传"],
  },
  "providers.auto_probe_interval": {
    group: "providers",
    title: "文字探活间隔",
    summary: "定时用一道简单算术题检查供应商是否可用。",
    detail: "0 表示只手动探活",
    kind: "integer",
    icon: Activity,
    unit: "秒",
    min: 0,
    max: 3600,
    defaultValue: "120",
    recommended: "默认：120 秒；账号少时可以更短。",
    keywords: ["provider", "probe", "探活"],
  },
  "providers.auto_image_probe_interval": {
    group: "providers",
    title: "图片探活间隔",
    summary: "定时生成一张测试图，确认图片生成能力真的可用。",
    detail: "自动巡检间隔（秒）",
    kind: "integer",
    icon: ImageIcon,
    unit: "秒",
    min: 0,
    max: 86400,
    defaultValue: "0",
    recommended: "默认：0，先关闭。",
    warning: "每次都消耗一次图片配额；生产建议关闭或 ≥ 30 分钟。",
    keywords: ["provider", "image", "probe", "图片探活"],
  },
  [UPDATE_USE_PROXY_POOL_KEY]: {
    group: "update",
    title: "更新时使用代理池",
    summary: "一键更新 Lumen 时，让 git、uv 和 npm 的出站请求走代理池。",
    detail: "仅影响一键更新",
    kind: "toggle",
    icon: Rocket,
    defaultValue: "0",
    recommended: "国内服务器拉取依赖慢或失败时开启。",
    keywords: ["update", "proxy", "更新", "代理池"],
  },
  [UPDATE_PROXY_NAME_KEY]: {
    group: "update",
    title: "更新代理",
    summary: "选择一键更新时使用代理池里的哪一个代理。",
    detail: "留空走第一个启用代理",
    kind: "text",
    icon: Rocket,
    recommended: "优先选择已测试成功、延迟稳定的代理。",
    keywords: ["update", "proxy", "name", "更新代理"],
  },
  [MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY]: {
    group: "library",
    title: "模特库同步使用代理池",
    summary: "同步模特库预设时，让 GitHub 文件列表和图片下载走代理池。",
    detail: "仅影响模特库同步",
    kind: "toggle",
    icon: ImageIcon,
    defaultValue: "0",
    recommended: "国内服务器同步 GitHub 预设失败时开启。",
    keywords: ["model", "library", "sync", "proxy", "模特库", "同步", "代理池"],
  },
  [MODEL_LIBRARY_SYNC_PROXY_NAME_KEY]: {
    group: "library",
    title: "模特库同步代理",
    summary: "选择模特库同步时使用代理池里的哪一个代理。",
    detail: "留空走第一个启用代理",
    kind: "text",
    icon: ImageIcon,
    recommended: "优先选择能稳定访问 GitHub 的代理。",
    keywords: ["model", "library", "sync", "proxy", "name", "模特库同步代理"],
  },
  "context.compression_enabled": {
    group: "context_auto",
    title: "自动压缩长对话",
    summary: "对话快超过上下文时，自动把较早内容整理成摘要。",
    detail: "效果取决于摘要模型",
    kind: "toggle",
    icon: BrainCircuit,
    defaultValue: "0",
    recommended: "长对话较多时打开。",
    keywords: ["context", "compression", "上下文", "压缩"],
  },
  "context.compression_trigger_percent": {
    group: "context_auto",
    title: "触发压缩的上下文占用",
    summary: "对话占用达到这个比例后，才会尝试自动压缩。",
    detail: "压缩触发阈值（%）",
    kind: "integer",
    icon: Gauge,
    unit: "%",
    min: 50,
    max: 98,
    defaultValue: "80",
    recommended: "默认：80%",
    keywords: ["trigger", "percent", "阈值"],
  },
  "context.summary_target_tokens": {
    group: "context_auto",
    title: "摘要保留长度",
    summary: "压缩后的摘要大概保留多少 token。",
    detail: "摘要 token 上限",
    kind: "integer",
    icon: SlidersHorizontal,
    unit: "token",
    min: 300,
    max: 8000,
    defaultValue: "1200",
    recommended: "默认：1200",
    keywords: ["summary", "tokens", "摘要"],
  },
  "context.summary_model": {
    group: "context_auto",
    title: "摘要模型",
    summary: "用于整理长对话摘要的模型 ID。",
    kind: "text",
    icon: Bot,
    defaultValue: "gpt-5.4",
    recommended: "选稳定、长上下文表现好的模型。",
    keywords: ["summary", "model", "摘要模型"],
  },
  "context.summary_min_recent_messages": {
    group: "context_auto",
    title: "最近原文保留条数",
    summary: "即使发生压缩，也至少保留最近这些消息的原文。",
    detail: "保底原文条数",
    kind: "integer",
    icon: ShieldCheck,
    unit: "条",
    min: 4,
    max: 64,
    defaultValue: "16",
    recommended: "默认：16 条",
    keywords: ["recent", "messages", "保留"],
  },
  "context.summary_min_interval_seconds": {
    group: "context_auto",
    title: "自动压缩冷却时间",
    summary: "同一会话两次自动压缩至少间隔多久。",
    detail: "防抖间隔（秒）",
    kind: "integer",
    icon: Timer,
    unit: "秒",
    min: 0,
    max: 3600,
    defaultValue: "30",
    recommended: "默认：30 秒",
    keywords: ["interval", "cooldown", "冷却"],
  },
  "context.summary_input_budget": {
    group: "context_auto",
    title: "单次摘要输入上限",
    summary: "一次摘要调用最多处理多少输入 token。",
    detail: "超出会分段汇总",
    kind: "integer",
    icon: SlidersHorizontal,
    unit: "token",
    min: 8000,
    max: 200000,
    defaultValue: "80000",
    recommended: "默认：80000",
    keywords: ["input", "budget", "摘要输入"],
  },
  "context.image_caption_enabled": {
    group: "context_caption",
    title: "图片离开上下文前自动描述",
    summary: "图片快被移出上下文时，先生成文字描述，方便后续对话继续引用。",
    kind: "toggle",
    icon: ImageIcon,
    defaultValue: "1",
    recommended: "多图长对话建议打开。",
    keywords: ["image", "caption", "图片描述"],
  },
  "context.image_caption_model": {
    group: "context_caption",
    title: "图片描述模型",
    summary: "用于给即将离开上下文的图片生成文字描述。",
    kind: "text",
    icon: Bot,
    defaultValue: "gpt-5.4-mini",
    recommended: "选成本低、稳定的视觉模型。",
    keywords: ["caption", "vision", "图片模型"],
  },
  "context.compression_circuit_breaker_threshold": {
    group: "context_auto",
    title: "摘要失败保护阈值",
    summary: "最近摘要失败比例超过这个值时，会暂停自动摘要一段时间。",
    detail: "失败率阈值（%）",
    kind: "integer",
    icon: ShieldCheck,
    unit: "%",
    min: 10,
    max: 100,
    defaultValue: "60",
    recommended: "默认：60%",
    keywords: ["circuit", "breaker", "失败保护"],
  },
  "context.manual_compact_min_input_tokens": {
    group: "context_manual",
    title: "允许手动压缩的门槛",
    summary: "会话输入量达到这个值后，用户才可以手动压缩。",
    detail: "门槛 token",
    kind: "integer",
    icon: BrainCircuit,
    unit: "token",
    min: 0,
    max: 200000,
    defaultValue: "4000",
    recommended: "默认：4000",
    keywords: ["manual", "compact", "手动压缩"],
  },
  "context.manual_compact_cooldown_seconds": {
    group: "context_manual",
    title: "手动压缩冷却时间",
    summary: "同一会话两次手动压缩至少间隔多久。",
    detail: "冷却时长（秒）",
    kind: "integer",
    icon: Timer,
    unit: "秒",
    min: 0,
    max: 86400,
    defaultValue: "600",
    recommended: "默认：600 秒，也就是 10 分钟。",
    keywords: ["manual", "cooldown", "手动压缩"],
  },
};

const GROUPS: {
  id: FilterId;
  label: string;
  description: string;
  icon: LucideIcon;
}[] = [
  {
    id: "all",
    label: "全部",
    description: "查看所有可调项",
    icon: SlidersHorizontal,
  },
  {
    id: "image",
    label: "图片生成",
    description: "引擎、通道和尺寸策略",
    icon: ImageIcon,
  },
  {
    id: "upstream",
    label: "对话与请求",
    description: "模型、并发和超时",
    icon: Zap,
  },
  {
    id: "context_auto",
    label: "长对话压缩",
    description: "自动摘要和熔断保护",
    icon: BrainCircuit,
  },
  {
    id: "context_caption",
    label: "图片描述",
    description: "出窗图片自动描述",
    icon: ImageIcon,
  },
  {
    id: "context_manual",
    label: "手动压缩",
    description: "用户主动压缩门槛",
    icon: BrainCircuit,
  },
  {
    id: "providers",
    label: "供应商探活",
    description: "自动检测账号可用性",
    icon: Activity,
  },
  {
    id: "library",
    label: "模特库",
    description: "预设同步和拉取代理",
    icon: ImageIcon,
  },
  {
    id: "update",
    label: "Lumen 更新",
    description: "一键更新和代理选择",
    icon: Rocket,
  },
  {
    id: "site",
    label: "站点",
    description: "域名和对外链接",
    icon: Globe,
  },
  {
    id: "advanced",
    label: "其他",
    description: "未归类设置",
    icon: Database,
  },
];

const GROUP_NAV_SECTIONS: {
  label: string;
  ids: FilterId[];
}[] = [
  { label: "核心", ids: ["all", "image", "upstream", "providers", "site"] },
  {
    label: "上下文",
    ids: ["context_auto", "context_caption", "context_manual"],
  },
  { label: "运维", ids: ["library", "advanced"] },
];

const SETTINGS_SKELETON_KEYS = [
  "settings-skeleton-summary",
  "settings-skeleton-image",
  "settings-skeleton-context",
] as const;

const settingInputClassName =
  "h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-3 text-sm text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-2)] focus:border-accent-border focus:ring-2 focus:ring-accent/20";

const settingMonoInputClassName = `${settingInputClassName} font-mono`;

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
    const errors: Record<string, string> = {};
    const payload: { key: string; value: string }[] = [];

    for (const [key, op] of Object.entries(ops)) {
      const meta = getSettingMeta(key);
      if (op.kind === "clear") {
        payload.push({ key, value: "" });
        continue;
      }

      const raw = op.value.trim();
      if (meta.kind === "integer" || meta.kind === "decimal") {
        if (raw === "") {
          errors[key] = "需填数值";
          continue;
        }
        const n = Number(raw);
        if (!Number.isFinite(n)) {
          errors[key] = "数字格式错误";
          continue;
        }
        if (meta.kind === "integer" && !Number.isInteger(n)) {
          errors[key] = "不支持小数";
          continue;
        }
        if (meta.min != null && n < meta.min) {
          errors[key] = `不能小于 ${formatPlainNumber(meta.min)}${meta.unit ?? ""}`;
          continue;
        }
        if (meta.max != null && n > meta.max) {
          errors[key] = `不能大于 ${formatPlainNumber(meta.max)}${meta.unit ?? ""}`;
          continue;
        }
        payload.push({
          key,
          value: meta.kind === "integer" ? String(Math.trunc(n)) : String(n),
        });
        continue;
      }

      if (meta.kind === "toggle") {
        if (raw !== "0" && raw !== "1") {
          errors[key] = "需选开启/关闭";
          continue;
        }
        payload.push({ key, value: raw });
        continue;
      }

      if (meta.kind === "enum") {
        if (!meta.choices?.some((option) => option.value === raw)) {
          errors[key] = "无效选项";
          continue;
        }
        payload.push({ key, value: raw });
        continue;
      }

      if (meta.kind === "url") {
        const normalized = normalizePublicBaseUrlInput(raw);
        if (!normalized) {
          errors[key] = "需 http(s) 根域名，无路径";
          continue;
        }
        payload.push({ key, value: normalized });
        continue;
      }

      if (raw === "") {
        errors[key] = copy.error.required;
        continue;
      }
      payload.push({ key, value: raw });
    }

    setFieldErrors(errors);
    return { ok: Object.keys(errors).length === 0, payload };
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

    updateMut.mutate(payload, {
      onSuccess: () => {
        setSavedAt(Date.now());
        setOps({});
        setFieldErrors({});
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
        <aside className="self-start rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/64 p-3 shadow-[var(--shadow-1)] backdrop-blur-sm lg:sticky lg:top-4">
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
            className="fixed bottom-0 left-0 right-0 z-40 max-w-full px-4 pb-[env(safe-area-inset-bottom)] sm:bottom-4 sm:left-1/2 sm:right-auto sm:w-auto sm:max-w-[calc(100vw-2rem)] sm:-translate-x-1/2 sm:px-0 sm:pb-4"
          >
            <div className="flex items-center gap-2 rounded-[var(--radius-dialog)] border border-accent-border bg-[var(--bg-1)]/95 px-3 py-2.5 shadow-[var(--shadow-3)] backdrop-blur-xl sm:gap-3 sm:px-4">
              <span className="inline-flex items-center gap-1.5 whitespace-nowrap type-caption text-[var(--fg-1)]">
                <span className="h-1.5 w-1.5 rounded-full bg-accent shadow-[var(--shadow-amber)]" />
                <span className="font-mono tabular-nums">{dirtyCount}</span>
                <span>项待保存</span>
              </span>
              <div className="flex-1 sm:flex-none" />
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

function SettingsOverviewCard({
  overview,
  dirtyCount,
  visibleCount,
}: {
  overview: {
    defaultModelLabel: string;
    engineLabel: string;
    channelLabel: string;
    formatLabel: string;
    compressionLabel: string;
  };
  dirtyCount: number;
  visibleCount: number;
}) {
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/70 p-4 shadow-[var(--shadow-2)] backdrop-blur-sm md:p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-card)] border border-accent-border bg-accent-soft">
            <SlidersHorizontal className="h-4 w-4 text-accent" />
          </div>
          <div className="min-w-0">
            <h2 className="type-card-title">系统配置概览</h2>
            <p className="mt-1 max-w-3xl type-body-sm text-[var(--fg-2)]">
              常用开关在这里先给出结果，下面再按任务分区编辑。数据库设置优先生效，保存后通常几秒内同步到 API 和 Worker。
            </p>
          </div>
        </div>
        <div className="inline-flex w-fit items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-3 py-1.5 type-caption text-[var(--fg-1)]">
          <Database className="h-3.5 w-3.5 text-[var(--fg-2)]" />
          {dirtyCount > 0 ? `${dirtyCount} 项待保存` : `${visibleCount} 项可配置`}
        </div>
      </div>

      <div className="mt-5 grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
        <OverviewMetric
          icon={Bot}
          label="默认模型"
          value={overview.defaultModelLabel}
        />
        <OverviewMetric
          icon={ImageIcon}
          label="生图引擎"
          value={overview.engineLabel}
        />
        <OverviewMetric
          icon={Activity}
          label="异步通道"
          value={overview.channelLabel}
        />
        <OverviewMetric
          icon={ImageIcon}
          label="输出格式"
          value={overview.formatLabel}
        />
        <OverviewMetric
          icon={BrainCircuit}
          label="自动压缩"
          value={overview.compressionLabel}
        />
      </div>
    </div>
  );
}

function SettingsSectionHeader({
  icon: Icon,
  title,
  description,
  badge,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  badge?: string;
}) {
  return (
    <div className="flex flex-col gap-3 border-b border-[var(--border-subtle)] pb-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex min-w-0 items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]">
          <Icon className="h-4 w-4 text-[var(--fg-1)]" />
        </div>
        <div className="min-w-0">
          <h3 className="type-card-title">{title}</h3>
          <p className="mt-1 type-body-sm text-[var(--fg-2)]">{description}</p>
        </div>
      </div>
      {badge && (
        <span className="w-fit rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-2.5 py-1 type-caption text-[var(--fg-1)]">
          {badge}
        </span>
      )}
    </div>
  );
}

function SettingsGroupNav({
  activeGroup,
  totalCount,
  groupCounts,
  onChange,
}: {
  activeGroup: FilterId;
  totalCount: number;
  groupCounts: Record<SettingGroupId, number>;
  onChange: (group: FilterId) => void;
}) {
  return (
    <div className="space-y-3" aria-label="系统设置分类">
      {GROUP_NAV_SECTIONS.map((section) => {
        const groupsInSection = section.ids
          .map((id) => GROUPS.find((group) => group.id === id))
          .filter((group): group is (typeof GROUPS)[number] => {
            if (!group) return false;
            const count =
              group.id === "all" ? totalCount : groupCounts[group.id] ?? 0;
            return group.id === "all" || count > 0;
          });
        if (groupsInSection.length === 0) return null;
        return (
          <div key={section.label}>
            <p className="mb-1.5 px-2 text-[10px] font-medium uppercase tracking-[0.08em] text-[var(--fg-3)]">
              {section.label}
            </p>
            <div className="space-y-1">
              {groupsInSection.map((group) => {
                const count =
                  group.id === "all" ? totalCount : groupCounts[group.id] ?? 0;
                const active = activeGroup === group.id;
                const Icon = group.icon;
                return (
                  <button
                    key={group.id}
                    type="button"
                    onClick={() => onChange(group.id)}
                    className={cn(
                      "flex min-h-[40px] w-full cursor-pointer items-center gap-2 rounded-[var(--radius-control)] border px-2.5 py-1.5 text-left transition-colors",
                      active
                        ? "border-accent-border bg-accent-soft text-[var(--fg-0)]"
                        : "border-transparent text-[var(--fg-1)] hover:border-[var(--border)] hover:bg-[var(--bg-2)]",
                    )}
                    title={group.description}
                  >
                    <Icon
                      className={cn(
                        "h-3.5 w-3.5 shrink-0",
                        active ? "text-accent" : "text-[var(--fg-2)]",
                      )}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate type-caption text-current">
                        {group.label}
                      </span>
                      <span className="mt-0.5 hidden truncate text-[11px] leading-4 text-[var(--fg-2)] xl:block">
                        {group.description}
                      </span>
                    </span>
                    <span
                      className={cn(
                        "shrink-0 rounded-full border px-1.5 py-0.5 font-mono text-[10px]",
                        active
                          ? "border-accent-border bg-[var(--bg-0)]/35 text-accent"
                          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]",
                      )}
                    >
                      {count}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SettingsGroup({
  group,
  ops,
  fieldErrors,
  dependencyState,
  modelsQuery,
  providerStatus,
  updateProxyOptions,
  onChange,
}: {
  group: { id: SettingGroupId; label: string; description: string; items: SystemSettingItem[] };
  ops: Record<string, Op>;
  fieldErrors: Record<string, string>;
  dependencyState: DependencyState;
  modelsQuery: ModelsQueryState;
  providerStatus: ProviderStatus;
  updateProxyOptions: UpdateProxyOption[];
  onChange: (key: string, op: Op | undefined) => void;
}) {
  const groupMeta = GROUPS.find((g) => g.id === group.id);
  const Icon = groupMeta?.icon ?? Database;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]">
            <Icon className="h-4 w-4 text-[var(--fg-1)]" />
          </div>
          <div className="min-w-0">
            <h3 className="type-card-title">
              {group.label}
            </h3>
            <p className="mt-0.5 type-caption text-[var(--fg-2)]">
              {group.description}
            </p>
          </div>
        </div>
        <span className="shrink-0 rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 font-mono text-[11px] text-[var(--fg-2)]">
          {group.items.length}
        </span>
      </div>

      <div className="grid gap-3">
        {group.id === "context_auto" && !dependencyState.compressionEnabled && (
          <DependencyNotice
            icon={BrainCircuit}
            title="先打开自动压缩"
            body="打开后再调整触发阈值、目标 token、模型和熔断参数。"
          />
        )}
        {group.items.map((item) => (
          <SettingCard
            key={item.key}
            item={item}
            op={ops[item.key]}
            fieldError={fieldErrors[item.key]}
            modelsQuery={modelsQuery}
            providerStatus={providerStatus}
            updateProxyOptions={updateProxyOptions}
            onChange={(op) => onChange(item.key, op)}
          />
        ))}
      </div>
    </div>
  );
}

function SettingCard({
  item,
  op,
  fieldError,
  modelsQuery,
  providerStatus,
  updateProxyOptions,
  onChange,
}: {
  item: SystemSettingItem;
  op: Op | undefined;
  fieldError: string | undefined;
  modelsQuery: ModelsQueryState;
  providerStatus: ProviderStatus;
  updateProxyOptions: UpdateProxyOption[];
  onChange: (op: Op | undefined) => void;
}) {
  const meta = getSettingMeta(item.key, item.description);
  const Icon = meta.icon;
  const isDirty = !!op;
  const displayValue = currentDisplayValue(item, op, meta);
  const hasDbOverride = item.value != null && item.value !== "";
  const [showDetails, setShowDetails] = useState(false);

  return (
    <motion.article
      layout
      transition={{ duration: 0.18 }}
      className={cn(
        "rounded-[var(--radius-card)] border p-3 backdrop-blur-sm transition-colors md:p-4",
        isDirty
          ? "border-accent-border bg-accent-soft shadow-[var(--shadow-1)]"
          : "border-[var(--border)] bg-[var(--bg-1)]/60",
      )}
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-accent-border bg-accent-soft">
            <Icon className="h-4 w-4 text-accent" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h4 className="type-body-sm font-medium text-[var(--fg-0)]">
                {meta.title}
              </h4>
              <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 text-[11px] text-[var(--fg-1)]">
                当前：{displayValue}
              </span>
              <SourceBadge
                hasDbOverride={hasDbOverride}
                hasAnyValue={item.has_value}
              />
            </div>
            <p className="mt-1 type-body-sm text-[var(--fg-2)]">
              {meta.summary}
            </p>
          </div>
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setShowDetails((value) => !value)}
          leftIcon={
            showDetails ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )
          }
          className="w-fit"
        >
          详情
        </Button>
      </div>

      {meta.warning && (
        <div className="mt-3 flex items-start gap-2 rounded-[var(--radius-control)] border border-warning-border bg-warning-soft px-3 py-2 type-caption leading-5 text-warning">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {meta.warning}
        </div>
      )}

      <div className="mt-3">
        <SettingControl
          item={item}
          meta={meta}
          op={op}
          modelsQuery={modelsQuery}
          providerStatus={providerStatus}
          updateProxyOptions={updateProxyOptions}
          onChange={onChange}
        />
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-[var(--fg-2)]">
        {meta.recommended && (
          <span className="rounded-[var(--radius-control)] border border-success-border bg-success-soft px-2 py-1 text-success">
            {meta.recommended}
          </span>
        )}
        {(meta.min != null || meta.max != null) && (
          <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1">
            范围 {meta.min != null ? formatPlainNumber(meta.min) : "不限"}
            {" 到 "}
            {meta.max != null ? formatPlainNumber(meta.max) : "不限"}
            {meta.unit ?? ""}
          </span>
        )}
      </div>

      <AnimatePresence initial={false}>
        {showDetails && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="mt-3 space-y-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-2 type-caption text-[var(--fg-2)]">
              {meta.detail && <p>{meta.detail}</p>}
              <p>
                技术名{" "}
                <code className="font-mono text-[var(--fg-1)]">{item.key}</code>
              </p>
              {item.description && item.description !== meta.summary && (
                <p>{item.description}</p>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {fieldError && (
        <p className="mt-3 flex items-center gap-1.5 type-caption text-danger">
          <AlertCircle className="h-3.5 w-3.5" /> {fieldError}
        </p>
      )}

      {op?.kind === "set" && (
        <p className="mt-3 text-xs text-[var(--color-lumen-amber)]/90">
          保存后改为：{formatValue(op.value, meta)}
        </p>
      )}
      {op?.kind === "clear" && (
        <p className="mt-3 text-xs text-[var(--color-lumen-amber)]/90">
          保存后清除该项
        </p>
      )}
    </motion.article>
  );
}

function SettingControl({
  item,
  meta,
  op,
  modelsQuery,
  providerStatus,
  updateProxyOptions,
  onChange,
}: {
  item: SystemSettingItem;
  meta: SettingMeta;
  op: Op | undefined;
  modelsQuery: ModelsQueryState;
  providerStatus: ProviderStatus;
  updateProxyOptions: UpdateProxyOption[];
  onChange: (op: Op | undefined) => void;
}) {
  const browserOrigin = useSyncExternalStore(
    subscribeStatic,
    getBrowserOrigin,
    getBrowserOriginSSR,
  );

  const controlValue =
    op?.kind === "clear"
      ? ""
      : op?.kind === "set"
        ? op.value
        : item.value ?? meta.defaultValue ?? "";
  const inputValue =
    op?.kind === "clear" ? "" : op?.kind === "set" ? op.value : item.value ?? "";
  const showDefaultAction =
    item.value != null &&
    item.value !== "" &&
    meta.defaultValue != null &&
    item.value !== meta.defaultValue;
  const [showAdvancedEngine, setShowAdvancedEngine] = useState(
    normalizeImageEngine(controlValue) === "dual_race",
  );

  if (meta.kind === "enum") {
    const isEngine = item.key === IMAGE_ENGINE_KEY;
    const normalizedValue = isEngine
      ? normalizeImageEngine(controlValue)
      : item.key === IMAGE_CHANNEL_KEY
        ? normalizeImageChannel(controlValue)
        : controlValue;
    const choices =
      isEngine && !showAdvancedEngine && normalizedValue !== "dual_race"
        ? (meta.choices ?? []).filter((option) => option.value !== "dual_race")
        : meta.choices ?? [];
    return (
      <div className="space-y-2">
        <div
          className="grid gap-2 md:grid-cols-3"
          role="radiogroup"
          aria-label={meta.title}
        >
          {choices.map((option) => {
            const selected = normalizedValue === option.value;
            return (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => onChange({ kind: "set", value: option.value })}
                className={cn(
                  "min-h-[72px] cursor-pointer rounded-[var(--radius-control)] border px-3 py-2 text-left transition-colors",
                  option.value === "dual_race"
                    ? "border-danger-border bg-danger-soft"
                    : selected
                      ? "border-accent-border bg-accent-soft text-[var(--fg-0)]"
                      : "border-[var(--border)] bg-[var(--bg-0)]/60 text-[var(--fg-1)] hover:bg-[var(--bg-2)]",
                )}
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="type-body-sm font-medium text-current">{option.label}</span>
                  {option.badge && (
                    <span
                      className={cn(
                        "rounded-full border px-2 py-0.5 text-[10px]",
                        option.value === "dual_race"
                          ? "border-danger-border bg-danger-soft text-danger"
                          : "border-warning-border bg-warning-soft text-warning",
                      )}
                    >
                      {option.badge}
                    </span>
                  )}
                </span>
                <span className="mt-1 block type-caption text-[var(--fg-2)]">
                  {option.description}
                </span>
              </button>
            );
          })}
        </div>
        {isEngine && !showAdvancedEngine && (
          <button
            type="button"
            onClick={() => setShowAdvancedEngine(true)}
            className="inline-flex min-h-[32px] cursor-pointer items-center gap-1 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-2 type-caption text-danger transition-colors hover:bg-danger/15"
          >
            <ChevronRight className="h-3.5 w-3.5" />
            显示进阶路径
          </button>
        )}
        {item.key === IMAGE_CHANNEL_KEY && (
          <p className="type-caption text-[var(--fg-2)]">{providerStatus.label}</p>
        )}
        <ResetEditButton
          dirty={!!op}
          defaultValue={meta.defaultValue}
          showDefaultAction={showDefaultAction}
          onReset={() => onChange(undefined)}
          onUseDefault={(value) => onChange({ kind: "set", value })}
        />
      </div>
    );
  }

  if (meta.kind === "model") {
    return (
      <ModelSelectControl
        item={item}
        meta={meta}
        op={op}
        modelsQuery={modelsQuery}
        showDefaultAction={showDefaultAction}
        onChange={onChange}
      />
    );
  }

  if (
    item.key === UPDATE_PROXY_NAME_KEY ||
    item.key === MODEL_LIBRARY_SYNC_PROXY_NAME_KEY
  ) {
    return (
      <UpdateProxySelectControl
        item={item}
        op={op}
        proxies={updateProxyOptions}
        onChange={onChange}
      />
    );
  }

  if (meta.kind === "toggle") {
    const checked = controlValue === "1";
    return (
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          role="switch"
          aria-checked={checked}
          aria-label={`${meta.title} ${checked ? "关闭" : "开启"}`}
          onClick={() => onChange({ kind: "set", value: checked ? "0" : "1" })}
          className={cn(
            "relative inline-flex h-7 w-12 shrink-0 cursor-pointer items-center rounded-full border transition-colors focus:outline-none focus:ring-2 focus:ring-accent/30",
            checked
              ? "border-accent-border bg-accent"
              : "border-[var(--border)] bg-[var(--bg-2)]",
          )}
        >
          <span
            aria-hidden
            className={cn(
              "inline-block h-5 w-5 rounded-full bg-[var(--bg-0)] shadow-[var(--shadow-1)] transition-transform",
              checked ? "translate-x-[22px]" : "translate-x-0.5",
            )}
          />
        </button>
        <span
          className={cn(
            "inline-flex rounded-[var(--radius-control)] border px-2 py-1 type-caption",
            checked
              ? "border-success-border bg-success-soft text-success"
              : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]",
          )}
        >
          {checked ? "开启" : "关闭"}
        </span>
        <ResetEditButton
          dirty={!!op}
          defaultValue={meta.defaultValue}
          showDefaultAction={showDefaultAction}
          onReset={() => onChange(undefined)}
          onUseDefault={(value) => onChange({ kind: "set", value })}
        />
      </div>
    );
  }

  if (meta.kind === "integer" || meta.kind === "decimal") {
    return (
      <div className="flex flex-col gap-2 md:flex-row md:items-center">
        <label htmlFor={`setting-${item.key}`} className="sr-only">
          {meta.title}
        </label>
        <div className="relative flex-1">
          <input
            id={`setting-${item.key}`}
            type="number"
            value={inputValue}
            min={meta.min}
            max={meta.max}
            step={meta.step ?? (meta.kind === "integer" ? 1 : "any")}
            onChange={(e) => {
              const value = e.target.value;
              onChange(value === "" ? undefined : { kind: "set", value });
            }}
            placeholder={
              meta.defaultValue
                ? `默认 ${formatValue(meta.defaultValue, meta)}`
                : "填写数值"
            }
            inputMode={meta.kind === "integer" ? "numeric" : "decimal"}
            className={`${settingMonoInputClassName} pr-16`}
          />
          {meta.unit && (
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 type-caption text-[var(--fg-2)]">
              {meta.unit}
            </span>
          )}
        </div>
        <ResetEditButton
          dirty={!!op}
          defaultValue={meta.defaultValue}
          showDefaultAction={showDefaultAction}
          onReset={() => onChange(undefined)}
          onUseDefault={(value) => onChange({ kind: "set", value })}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 md:flex-row md:items-center">
      <label htmlFor={`setting-${item.key}`} className="sr-only">
        {meta.title}
      </label>
      <input
        id={`setting-${item.key}`}
        type={meta.kind === "url" ? "url" : "text"}
        value={inputValue}
        onChange={(e) => {
          const value = e.target.value;
          onChange(value === "" ? undefined : { kind: "set", value });
        }}
        placeholder={
          meta.kind === "url"
            ? "https://example.com"
            : meta.defaultValue
              ? `默认 ${meta.defaultValue}`
              : "填写内容"
        }
        autoComplete="off"
        className={`flex-1 ${settingMonoInputClassName}`}
      />
      {meta.kind === "url" && browserOrigin && (
        <Button
          variant="secondary"
          size="sm"
          onClick={() => onChange({ kind: "set", value: browserOrigin })}
          leftIcon={<Globe className="h-3.5 w-3.5" />}
        >
          填入当前域名
        </Button>
      )}
      <ResetEditButton
        dirty={!!op}
        defaultValue={meta.defaultValue}
        showDefaultAction={showDefaultAction}
        onReset={() => onChange(undefined)}
        onUseDefault={(value) => onChange({ kind: "set", value })}
      />
    </div>
  );
}

function ModelSelectControl({
  item,
  meta,
  op,
  modelsQuery,
  showDefaultAction,
  onChange,
}: {
  item: SystemSettingItem;
  meta: SettingMeta;
  op: Op | undefined;
  modelsQuery: ModelsQueryState;
  showDefaultAction: boolean;
  onChange: (op: Op | undefined) => void;
}) {
  const modelIds = useMemo(() => {
    const ids = new Set<string>();
    if (meta.defaultValue) ids.add(meta.defaultValue);
    for (const model of modelsQuery.models) ids.add(model);
    return Array.from(ids).sort();
  }, [meta.defaultValue, modelsQuery.models]);
  const value =
    op?.kind === "clear" ? "" : op?.kind === "set" ? op.value : item.value ?? "";
  const effective = value || meta.defaultValue || "";
  const [customMode, setCustomMode] = useState(
    Boolean(effective && !modelIds.includes(effective)),
  );
  const inputValue =
    op?.kind === "clear" ? "" : op?.kind === "set" ? op.value : item.value ?? "";

  if (modelsQuery.isError || modelIds.length === 0) {
    return (
      <div className="space-y-2">
        <div className="flex flex-col gap-2 md:flex-row md:items-center">
          <TextSettingInput
            item={item}
            meta={meta}
            value={inputValue}
            onChange={onChange}
          />
          <ResetEditButton
            dirty={!!op}
            defaultValue={meta.defaultValue}
            showDefaultAction={showDefaultAction}
            onReset={() => onChange(undefined)}
            onUseDefault={(defaultValue) =>
              onChange({ kind: "set", value: defaultValue })
            }
          />
        </div>
        <p className="type-caption text-warning">
          模型列表读取失败，已切换为手动输入
          {modelsQuery.errorMessage ? `：${modelsQuery.errorMessage}` : ""}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 md:flex-row md:items-center">
      {customMode ? (
        <TextSettingInput
          item={item}
          meta={meta}
          value={inputValue}
          onChange={onChange}
        />
      ) : (
        <select
          value={modelIds.includes(effective) ? effective : "__custom__"}
          onChange={(event) => {
            const next = event.target.value;
            if (next === "__custom__") {
              setCustomMode(true);
              return;
            }
            onChange({ kind: "set", value: next });
          }}
          className={`flex-1 ${settingMonoInputClassName}`}
        >
          {modelIds.map((model) => (
            <option key={model} value={model}>
              {model}
            </option>
          ))}
          <option value="__custom__">自定义...</option>
        </select>
      )}
      {customMode && (
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setCustomMode(false)}
        >
          返回列表
        </Button>
      )}
      <ResetEditButton
        dirty={!!op}
        defaultValue={meta.defaultValue}
        showDefaultAction={showDefaultAction}
        onReset={() => onChange(undefined)}
        onUseDefault={(defaultValue) => onChange({ kind: "set", value: defaultValue })}
      />
      {modelsQuery.isLoading && (
        <span className="inline-flex items-center gap-1 type-caption text-[var(--fg-2)]">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          模型列表读取中
        </span>
      )}
    </div>
  );
}

function UpdateProxySelectControl({
  item,
  op,
  proxies,
  onChange,
}: {
  item: SystemSettingItem;
  op: Op | undefined;
  proxies: UpdateProxyOption[];
  onChange: (op: Op | undefined) => void;
}) {
  const value =
    op?.kind === "clear" ? "" : op?.kind === "set" ? op.value : item.value ?? "";
  const enabledProxies = proxies.filter((proxy) => proxy.enabled);
  const selectedExists = !value || enabledProxies.some((proxy) => proxy.name === value);
  const proxyFeatureLabel =
    item.key === MODEL_LIBRARY_SYNC_PROXY_NAME_KEY
      ? "模特库同步使用代理池"
      : "更新时使用代理池";

  return (
    <div className="space-y-2">
      <div className="flex flex-col gap-2 md:flex-row md:items-center">
        <select
          value={selectedExists ? value : "__custom__"}
          onChange={(event) => {
            const next = event.target.value;
            if (next === "") {
              onChange(item.value ? { kind: "clear" } : undefined);
            } else {
              onChange({ kind: "set", value: next });
            }
          }}
          className={`flex-1 ${settingInputClassName}`}
        >
          <option value="">自动选择第一个启用代理</option>
          {enabledProxies.map((proxy) => (
            <option key={proxy.name} value={proxy.name}>
              {proxy.name}
              {proxy.last_latency_ms != null
                ? ` · ${Math.round(proxy.last_latency_ms)}ms`
                : ""}
              {proxy.in_cooldown ? " · 冷却中" : ""}
            </option>
          ))}
          {!selectedExists && <option value="__custom__">{value}</option>}
        </select>
        <ResetEditButton
          dirty={!!op}
          defaultValue={undefined}
          showDefaultAction={false}
          onReset={() => onChange(undefined)}
          onUseDefault={() => {}}
        />
      </div>
      {enabledProxies.length === 0 ? (
        <p className="type-caption text-warning">
          代理池没有启用代理；开启“{proxyFeatureLabel}”后，请求会被后端拒绝。
        </p>
      ) : (
        <p className="type-caption text-[var(--fg-2)]">
          可用代理 {enabledProxies.length} 个，选择后记得保存设置。
        </p>
      )}
    </div>
  );
}

function TextSettingInput({
  item,
  meta,
  value,
  onChange,
}: {
  item: SystemSettingItem;
  meta: SettingMeta;
  value: string;
  onChange: (op: Op | undefined) => void;
}) {
  return (
    <>
      <label htmlFor={`setting-${item.key}`} className="sr-only">
        {meta.title}
      </label>
      <input
        id={`setting-${item.key}`}
        type={meta.kind === "url" ? "url" : "text"}
        value={value}
        onChange={(e) => {
          const next = e.target.value;
          onChange(next === "" ? undefined : { kind: "set", value: next });
        }}
        placeholder={
          meta.kind === "url"
            ? "https://example.com"
            : meta.defaultValue
              ? `默认 ${meta.defaultValue}`
              : "填写内容"
        }
        autoComplete="off"
        className={`flex-1 ${settingMonoInputClassName}`}
      />
    </>
  );
}

function ResetEditButton({
  dirty,
  defaultValue,
  showDefaultAction,
  onReset,
  onUseDefault,
}: {
  dirty: boolean;
  defaultValue: string | undefined;
  showDefaultAction: boolean;
  onReset: () => void;
  onUseDefault: (value: string) => void;
}) {
  if (dirty) {
    return (
      <Button
        variant="secondary"
        size="sm"
        onClick={onReset}
        leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
      >
        撤销修改
      </Button>
    );
  }
  if (!defaultValue || !showDefaultAction) return null;
  return (
    <Button
      variant="secondary"
      size="sm"
      onClick={() => onUseDefault(defaultValue)}
      leftIcon={<Check className="h-3.5 w-3.5" />}
    >
      填入默认值
    </Button>
  );
}

function ContextHealthBlock({
  data,
  loading,
  error,
  onRetry,
}: {
  data: Awaited<ReturnType<typeof getAdminContextHealth>> | undefined;
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
}) {
  const successRate =
    data?.last_24h.summary_success_rate == null
      ? null
      : `${Math.round(data.last_24h.summary_success_rate * 1000) / 10}%`;
  const state = formatCircuitState(data?.circuit_breaker_state);

  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)] backdrop-blur-sm">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-card)] border border-info-border bg-info-soft">
            <ShieldCheck className="h-4 w-4 text-info" />
          </div>
          <div className="min-w-0">
            <h3 className="type-card-title text-sm">
              长对话摘要状态
            </h3>
            <p className="mt-1 type-caption text-[var(--fg-2)]">
              用来判断自动摘要是否稳定。这里是只读状态，不需要手动保存。
            </p>
          </div>
        </div>
        {loading ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-[var(--fg-1)]">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> 读取中
          </span>
        ) : error ? (
          <Button
            variant="secondary"
            size="sm"
            onClick={onRetry}
            leftIcon={<RotateCcw className="h-3 w-3" />}
          >
            {copy.action.retry}
          </Button>
        ) : (
          <span
            className={cn(
              "inline-flex items-center rounded-[var(--radius-control)] border px-2 py-0.5 text-xs",
              state.tone === "danger"
                ? "border-danger-border bg-danger-soft text-danger"
                : state.tone === "warning"
                  ? "border-warning-border bg-warning-soft text-warning"
                  : "border-success-border bg-success-soft text-success",
            )}
          >
            {state.label}
          </span>
        )}
      </div>

      {error ? (
        <p role="alert" className="mt-3 type-caption text-[var(--fg-2)]">
          暂时读不到摘要状态：{error.message}
        </p>
      ) : data ? (
        <div className="mt-4 grid grid-cols-2 gap-2 md:grid-cols-4">
          <HealthMetric label="摘要成功率" value={successRate ?? "暂无数据"} />
          <HealthMetric
            label="自动摘要次数"
            value={String(data.last_24h.summary_attempts)}
          />
          <HealthMetric
            label="P95 响应时间"
            value={
              data.last_24h.summary_p95_latency_ms == null
                ? "暂无数据"
                : `${data.last_24h.summary_p95_latency_ms}ms`
            }
          />
          <HealthMetric
            label="手动压缩次数"
            value={String(data.last_24h.manual_compact_calls)}
          />
        </div>
      ) : null}

      {data?.circuit_breaker_until && (
        <p className="mt-3 type-caption text-warning">
          自动摘要预计恢复时间：{data.circuit_breaker_until}
        </p>
      )}
    </div>
  );
}

function OverviewMetric({
  icon: Icon,
  label,
  value,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-2.5">
      <div className="flex items-center gap-2 text-[11px] text-[var(--fg-2)]">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <p className="mt-1 truncate text-sm font-medium text-[var(--fg-0)]">
        {value}
      </p>
    </div>
  );
}

function HealthMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-2">
      <p className="text-[11px] text-[var(--fg-2)]">{label}</p>
      <p className="mt-1 font-mono text-sm text-[var(--fg-0)]">{value}</p>
    </div>
  );
}

function DependencyNotice({
  icon: Icon,
  title,
  body,
}: {
  icon: LucideIcon;
  title: string;
  body: string;
}) {
  return (
    <div className="flex items-start gap-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-3 text-sm text-[var(--fg-1)]">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]">
        <Icon className="h-4 w-4 text-[var(--fg-2)]" />
      </div>
      <div>
        <p className="font-medium text-[var(--fg-0)]">{title}</p>
        <p className="mt-1 type-caption text-[var(--fg-2)]">{body}</p>
      </div>
    </div>
  );
}

function SourceBadge({
  hasDbOverride,
  hasAnyValue,
}: {
  hasDbOverride: boolean;
  hasAnyValue: boolean;
}) {
  if (hasDbOverride) {
    return (
      <span className="rounded-[var(--radius-control)] border border-accent-border bg-accent-soft px-2 py-0.5 text-[11px] text-accent">
        已覆盖默认
      </span>
    );
  }
  if (hasAnyValue) {
    return (
      <span className="rounded-[var(--radius-control)] border border-info-border bg-info-soft px-2 py-0.5 text-[11px] text-info">
        使用环境变量
      </span>
    );
  }
  return (
    <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 text-[11px] text-[var(--fg-2)]">
      使用程序默认
    </span>
  );
}

const AUTO_COMPRESSION_CHILD_KEYS = new Set([
  "context.compression_trigger_percent",
  "context.summary_target_tokens",
  "context.summary_model",
  "context.summary_min_recent_messages",
  "context.summary_min_interval_seconds",
  "context.summary_input_budget",
  "context.compression_circuit_breaker_threshold",
]);

function shouldRenderSetting(key: string, state: DependencyState) {
  if (
    key === IMAGE_JOB_BASE_URL_KEY &&
    normalizeImageChannel(state.imageChannel) === "stream_only"
  ) {
    return false;
  }
  if (key === "context.image_caption_model" && !state.imageCaptionEnabled) {
    return false;
  }
  if (AUTO_COMPRESSION_CHILD_KEYS.has(key) && !state.compressionEnabled) {
    return false;
  }
  return true;
}

function groupSettings(
  items: SystemSettingItem[],
  activeGroup: FilterId,
  search: string,
): {
  id: SettingGroupId;
  label: string;
  description: string;
  items: SystemSettingItem[];
}[] {
  const normalizedSearch = search.trim().toLowerCase();
  const map = new Map<SettingGroupId, SystemSettingItem[]>();

  for (const item of items) {
    const meta = getSettingMeta(item.key, item.description);
    if (activeGroup !== "all" && meta.group !== activeGroup) continue;
    if (normalizedSearch && !matchesSearch(item, meta, normalizedSearch)) {
      continue;
    }
    if (!map.has(meta.group)) map.set(meta.group, []);
    map.get(meta.group)!.push(item);
  }

  return GROUPS.filter(
    (group): group is (typeof GROUPS)[number] & { id: SettingGroupId } =>
      group.id !== "all",
  )
    .map((group) => ({
      id: group.id,
      label: group.label,
      description: group.description,
      items: map.get(group.id) ?? [],
    }))
    .filter((group) => group.items.length > 0);
}

function countByGroup(items: SystemSettingItem[]): Record<SettingGroupId, number> {
  const counts: Record<SettingGroupId, number> = {
    site: 0,
    image: 0,
    upstream: 0,
    providers: 0,
    library: 0,
    update: 0,
    context_auto: 0,
    context_caption: 0,
    context_manual: 0,
    advanced: 0,
  };
  for (const item of items) {
    counts[getSettingMeta(item.key, item.description).group] += 1;
  }
  return counts;
}

function matchesSearch(
  item: SystemSettingItem,
  meta: SettingMeta,
  normalizedSearch: string,
) {
  const haystack = [
    item.key,
    item.description,
    meta.title,
    meta.summary,
    meta.detail,
    meta.recommended,
    ...(meta.keywords ?? []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(normalizedSearch);
}

function getSettingMeta(key: string, fallbackDescription?: string): SettingMeta {
  const meta = SETTING_META[key];
  if (meta) return meta;
  const prefix = key.includes(".") ? key.split(".")[0] : key;
  let group: SettingGroupId = "advanced";
  if (prefix === "site") group = "site";
  else if (prefix === "image") group = "image";
  else if (prefix === "upstream") group = "upstream";
  else if (prefix === "providers") group = "providers";
  else if (prefix === "model_library") group = "library";
  else if (prefix === "update") group = "update";
  else if (prefix === "context") group = "context_auto";
  return {
    group,
    title: humanizeKey(key),
    summary: fallbackDescription || "未归类设置。修改前先确认影响范围。",
    kind: "text",
    icon: Database,
    keywords: [prefix],
  };
}

function currentDisplayValue(
  item: SystemSettingItem,
  op: Op | undefined,
  meta: SettingMeta,
) {
  if (op?.kind === "set") return formatValue(op.value, meta);
  if (op?.kind === "clear") return "未设置";
  if (item.value != null && item.value !== "") return formatValue(item.value, meta);
  if (item.has_value) return "来自环境变量";
  if (meta.defaultValue != null) return `默认 ${formatValue(meta.defaultValue, meta)}`;
  return "未设置";
}

function effectiveValue(
  item: SystemSettingItem | undefined,
  op: Op | undefined,
  defaultValue: string,
) {
  if (op?.kind === "set") return op.value;
  if (op?.kind === "clear") return "";
  return item?.value ?? defaultValue;
}

function isEnvOnlyValue(item: SystemSettingItem | undefined, op: Op | undefined) {
  return !op && item?.has_value === true && (item.value == null || item.value === "");
}

function normalizeImageEngine(value: string | null | undefined) {
  if (value === "image2") return "image2";
  if (value === "dual_race") return "dual_race";
  return "responses";
}

function normalizeImageChannel(value: string | null | undefined) {
  if (value === "stream_only") return "stream_only";
  if (value === "image_jobs_only") return "image_jobs_only";
  return "auto";
}

function subscribeStatic() {
  return () => {};
}

function getBrowserOrigin() {
  return typeof window === "undefined" ? null : window.location.origin;
}

function getBrowserOriginSSR() {
  return null;
}

function engineChoiceLabel(value: string | null | undefined) {
  const normalized = normalizeImageEngine(value);
  return (
    IMAGE_ENGINE_OPTIONS.find((option) => option.value === normalized)?.label ??
    "原生通道"
  );
}

function channelChoiceLabel(value: string | null | undefined) {
  const normalized = normalizeImageChannel(value);
  return (
    IMAGE_CHANNEL_OPTIONS.find((option) => option.value === normalized)?.label ??
    "自动混合"
  );
}

function outputFormatChoiceLabel(value: string | null | undefined) {
  const normalized = value === "png" ? "png" : "jpeg";
  return (
    IMAGE_OUTPUT_FORMAT_OPTIONS.find((option) => option.value === normalized)?.label ??
    "JPG 格式"
  );
}

function formatValue(value: string, meta: SettingMeta) {
  if (meta.kind === "toggle") return value === "1" ? "开启" : "关闭";
  if (meta.kind === "enum") {
    if (meta.choices === IMAGE_ENGINE_OPTIONS) return engineChoiceLabel(value);
    if (meta.choices === IMAGE_CHANNEL_OPTIONS) return channelChoiceLabel(value);
    if (meta.choices === IMAGE_OUTPUT_FORMAT_OPTIONS) {
      return outputFormatChoiceLabel(value);
    }
    return meta.choices?.find((option) => option.value === value)?.label ?? value;
  }
  if (meta.kind === "integer" || meta.kind === "decimal") {
    const n = Number(value);
    const formatted = Number.isFinite(n) ? formatPlainNumber(n) : value;
    return meta.unit ? `${formatted}${meta.unit}` : formatted;
  }
  return value;
}

function normalizePublicBaseUrlInput(value: string) {
  const raw = value.trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (url.username || url.password || url.search || url.hash) return null;
    if (url.pathname !== "" && url.pathname !== "/") return null;
    return url.origin;
  } catch {
    return null;
  }
}

function formatPlainNumber(value: number) {
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 3,
  }).format(value);
}

function formatDateTime(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function formatCircuitState(state: string | undefined): {
  label: string;
  tone: "success" | "warning" | "danger";
} {
  if (state === "open") return { label: "暂停摘要", tone: "danger" };
  if (state === "half_open") return { label: "试探恢复", tone: "warning" };
  if (state === "closed") return { label: "运行正常", tone: "success" };
  return { label: state || "未知状态", tone: "warning" };
}

function humanizeKey(key: string) {
  return key
    .split(".")
    .map((part) => part.replace(/_/g, " "))
    .join(" / ");
}
