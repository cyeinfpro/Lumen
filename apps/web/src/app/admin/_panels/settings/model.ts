import {
  Activity,
  Bot,
  BrainCircuit,
  Database,
  Gauge,
  Globe,
  ImageIcon,
  ShieldCheck,
  SlidersHorizontal,
  Timer,
  Zap,
  type LucideIcon,
} from "lucide-react";
import type { SystemSettingItem } from "@/lib/types";
import { CANVAS_ENABLED_KEY, CANVAS_SETTING_META } from "../canvasSettingMeta";

export type Op = { kind: "set"; value: string } | { kind: "clear" };

export type SettingGroupId =
  | "site"
  | "ui"
  | "image"
  | "upstream"
  | "providers"
  | "library"
  | "context_auto"
  | "context_caption"
  | "context_manual"
  | "advanced";
export type FilterId = "all" | SettingGroupId;
export type ValueKind =
  | "integer"
  | "decimal"
  | "text"
  | "url"
  | "toggle"
  | "enum"
  | "model";

export type SettingChoice = {
  value: string;
  label: string;
  description: string;
  badge?: string;
};

export type SettingMeta = {
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
  keywords?: readonly string[];
};

export type DependencyState = {
  imageChannel: string;
  compressionEnabled: boolean;
  imageCaptionEnabled: boolean;
};

export type ModelsQueryState = {
  isLoading: boolean;
  isError: boolean;
  errorMessage?: string;
  models: string[];
};

export type ProviderStatus = {
  total: number;
  jobs: number;
  label: string;
  compact: string;
};

export type UpdateProxyOption = {
  name: string;
  enabled: boolean;
  in_cooldown: boolean;
  last_latency_ms: number | null;
};

export const MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY = "model_library.sync_use_proxy_pool";
export const MODEL_LIBRARY_SYNC_PROXY_NAME_KEY = "model_library.sync_proxy_name";
export const GENERATION_FAST_DEFAULT_KEY = "generation.fast_default";
export const IMAGE_ENGINE_KEY = "image.engine";
export const IMAGE_CHANNEL_KEY = "image.channel";
export const IMAGE_GENERATION_CONCURRENCY_KEY = "image.generation_concurrency";
export const IMAGE_OUTPUT_FORMAT_KEY = "image.output_format";
export const IMAGE_JOB_BASE_URL_KEY = "image.job_base_url";
export const SITE_PUBLIC_BASE_URL_KEY = "site.public_base_url";
export const SITE_SHARE_EXPIRATION_DAYS_KEY = "site.share_expiration_days";
export const UI_NAV_STUDIO_VISIBLE_KEY = "ui.nav.studio_visible";
export const UI_NAV_VIDEO_VISIBLE_KEY = "ui.nav.video_visible";
export const UI_NAV_PROJECTS_VISIBLE_KEY = "ui.nav.projects_visible";
export const UI_NAV_ASSETS_VISIBLE_KEY = "ui.nav.assets_visible";
export const HIDDEN_KEYS = new Set<string>([
  "providers",
  "image.primary_route",
  "image.text_to_image_primary_route",
  "update.use_proxy_pool",
  "update.proxy_name",
]);

export const IMAGE_ENGINE_OPTIONS: readonly SettingChoice[] = [
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

export const IMAGE_CHANNEL_OPTIONS: readonly SettingChoice[] = [
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

export const IMAGE_OUTPUT_FORMAT_OPTIONS: readonly SettingChoice[] = [
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

export const SETTING_META: Record<string, SettingMeta> = {
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
  [UI_NAV_STUDIO_VISIBLE_KEY]: {
    group: "ui",
    title: "显示创作入口",
    summary: "控制主导航里的「创作」是否向用户显示。",
    detail: "关闭后用户访问创作页会自动跳到第一个可见入口。",
    kind: "toggle",
    icon: SlidersHorizontal,
    defaultValue: "1",
    recommended: "默认显示。四个业务入口可分别关闭。",
    keywords: ["ui", "nav", "studio", "创作", "入口", "导航"],
  },
  [UI_NAV_VIDEO_VISIBLE_KEY]: {
    group: "ui",
    title: "显示视频入口",
    summary: "控制主导航里的「视频」是否向用户显示。",
    detail: "关闭后视频页不会出现在顶部导航、移动底栏和命令面板中。",
    kind: "toggle",
    icon: SlidersHorizontal,
    defaultValue: "1",
    recommended: "未开放视频能力或只做图片时可以关闭。",
    keywords: ["ui", "nav", "video", "视频", "入口", "导航"],
  },
  [UI_NAV_PROJECTS_VISIBLE_KEY]: {
    group: "ui",
    title: "显示项目入口",
    summary: "控制主导航里的「项目」是否向用户显示。",
    detail: "关闭后项目中心及项目子页会从用户导航入口中隐藏。",
    kind: "toggle",
    icon: SlidersHorizontal,
    defaultValue: "1",
    recommended: "项目流程未准备好时可以单独关闭。",
    keywords: ["ui", "nav", "projects", "项目", "入口", "导航"],
  },
  [UI_NAV_ASSETS_VISIBLE_KEY]: {
    group: "ui",
    title: "显示素材入口",
    summary: "控制主导航里的「素材」是否向用户显示。",
    detail: "关闭后活动、图库和素材库入口会从用户导航中隐藏。",
    kind: "toggle",
    icon: SlidersHorizontal,
    defaultValue: "1",
    recommended: "不希望用户浏览历史素材时可以关闭。",
    keywords: ["ui", "nav", "assets", "stream", "library", "资产", "图库", "入口"],
  },
  [CANVAS_ENABLED_KEY]: CANVAS_SETTING_META,
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

export const GROUPS: {
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
    id: "ui",
    label: "界面入口",
    description: "创作、视频、项目和素材显示",
    icon: SlidersHorizontal,
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

export const GROUP_NAV_SECTIONS: {
  label: string;
  ids: FilterId[];
}[] = [
  { label: "核心", ids: ["all", "image", "upstream", "providers", "site", "ui"] },
  {
    label: "上下文",
    ids: ["context_auto", "context_caption", "context_manual"],
  },
  { label: "运维", ids: ["library", "advanced"] },
];

export const SETTINGS_SKELETON_KEYS = [
  "settings-skeleton-summary",
  "settings-skeleton-image",
  "settings-skeleton-context",
] as const;

export const settingInputClassName =
  "h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-3 text-sm text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-2)] focus:border-accent-border focus:ring-2 focus:ring-accent/20";

export const settingMonoInputClassName = `${settingInputClassName} font-mono`;


export const AUTO_COMPRESSION_CHILD_KEYS = new Set([
  "context.compression_trigger_percent",
  "context.summary_target_tokens",
  "context.summary_model",
  "context.summary_min_recent_messages",
  "context.summary_min_interval_seconds",
  "context.summary_input_budget",
  "context.compression_circuit_breaker_threshold",
]);

export function shouldRenderSetting(key: string, state: DependencyState) {
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

export function groupSettings(
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

export function countByGroup(items: SystemSettingItem[]): Record<SettingGroupId, number> {
  const counts: Record<SettingGroupId, number> = {
    site: 0,
    ui: 0,
    image: 0,
    upstream: 0,
    providers: 0,
    library: 0,
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

export function matchesSearch(
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

export function getSettingMeta(key: string, fallbackDescription?: string): SettingMeta {
  const meta = SETTING_META[key];
  if (meta) return meta;
  const prefix = key.includes(".") ? key.split(".")[0] : key;
  let group: SettingGroupId = "advanced";
  if (prefix === "site") group = "site";
  else if (prefix === "image") group = "image";
  else if (prefix === "upstream") group = "upstream";
  else if (prefix === "providers") group = "providers";
  else if (prefix === "model_library") group = "library";
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

export function currentDisplayValue(
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

export function effectiveValue(
  item: SystemSettingItem | undefined,
  op: Op | undefined,
  defaultValue: string,
) {
  if (op?.kind === "set") return op.value;
  if (op?.kind === "clear") return "";
  return item?.value ?? defaultValue;
}

export function isEnvOnlyValue(item: SystemSettingItem | undefined, op: Op | undefined) {
  return !op && item?.has_value === true && (item.value == null || item.value === "");
}

export function normalizeImageEngine(value: string | null | undefined) {
  if (value === "image2") return "image2";
  if (value === "dual_race") return "dual_race";
  return "responses";
}

export function normalizeImageChannel(value: string | null | undefined) {
  if (value === "stream_only") return "stream_only";
  if (value === "image_jobs_only") return "image_jobs_only";
  return "auto";
}

export function subscribeStatic() {
  return () => {};
}

export function getBrowserOrigin() {
  return typeof window === "undefined" ? null : window.location.origin;
}

export function getBrowserOriginSSR() {
  return null;
}

export function engineChoiceLabel(value: string | null | undefined) {
  const normalized = normalizeImageEngine(value);
  return (
    IMAGE_ENGINE_OPTIONS.find((option) => option.value === normalized)?.label ??
    "原生通道"
  );
}

export function channelChoiceLabel(value: string | null | undefined) {
  const normalized = normalizeImageChannel(value);
  return (
    IMAGE_CHANNEL_OPTIONS.find((option) => option.value === normalized)?.label ??
    "自动混合"
  );
}

export function outputFormatChoiceLabel(value: string | null | undefined) {
  const normalized = value === "png" ? "png" : "jpeg";
  return (
    IMAGE_OUTPUT_FORMAT_OPTIONS.find((option) => option.value === normalized)?.label ??
    "JPG 格式"
  );
}

export function formatValue(value: string, meta: SettingMeta) {
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

export function normalizePublicBaseUrlInput(value: string) {
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

export function formatPlainNumber(value: number) {
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 3,
  }).format(value);
}

export function formatCircuitState(state: string | undefined): {
  label: string;
  tone: "success" | "warning" | "danger";
} {
  if (state === "open") return { label: "暂停摘要", tone: "danger" };
  if (state === "half_open") return { label: "试探恢复", tone: "warning" };
  if (state === "closed") return { label: "运行正常", tone: "success" };
  return { label: state || "未知状态", tone: "warning" };
}

export function humanizeKey(key: string) {
  return key
    .split(".")
    .map((part) => part.replace(/_/g, " "))
    .join(" / ");
}
