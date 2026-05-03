"use client";

// Lumen 管理面板：系统设置。
// UI 目标：把工程 key 翻译成可理解的任务语言，同时保留 key 作为排错辅助信息。

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  AlertCircle,
  Bot,
  BrainCircuit,
  ChevronDown,
  ChevronRight,
  Check,
  Circle,
  Database,
  Gauge,
  Globe,
  History,
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
  Terminal,
  Timer,
  Undo2,
  X,
  Zap,
  type LucideIcon,
} from "lucide-react";

import {
  qk,
  useAdminModelsQuery,
  useAdminProxiesQuery,
  useAdminReleasesQuery,
  useAdminUpdateStatusQuery,
  useProvidersQuery,
  useRollbackReleaseMutation,
  useSystemSettingsQuery,
  useTriggerAdminUpdateMutation,
  useUpdateSystemSettingsMutation,
} from "@/lib/queries";
import {
  adminUpdateStreamUrl,
  ApiError,
  getAdminContextHealth,
  type AdminUpdateStatusOut,
  type ReleaseInfo,
  type UpdateStepRecord,
} from "@/lib/apiClient";
import type { SystemSettingItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ConfirmDialog } from "@/components/ui/primitives";
import { ErrorBlock } from "../page";

type Op = { kind: "set"; value: string } | { kind: "clear" };
type SettingGroupId =
  | "site"
  | "image"
  | "upstream"
  | "providers"
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
const IMAGE_ENGINE_KEY = "image.engine";
const IMAGE_CHANNEL_KEY = "image.channel";
const IMAGE_OUTPUT_FORMAT_KEY = "image.output_format";
const IMAGE_JOB_BASE_URL_KEY = "image.job_base_url";
const SITE_PUBLIC_BASE_URL_KEY = "site.public_base_url";
const SITE_SHARE_EXPIRATION_DAYS_KEY = "site.share_expiration_days";
const HIDDEN_KEYS = new Set<string>([
  "providers",
  "image.primary_route",
  "image.text_to_image_primary_route",
]);

const IMAGE_ENGINE_OPTIONS: readonly SettingChoice[] = [
  {
    value: "responses",
    label: "Codex 原生",
    description: "默认路径。走 Codex 原生生图链路，适合日常文生图和图生图。",
  },
  {
    value: "image2",
    label: "image2 直连",
    description: "直接调用图像接口，简单任务更快；4K 图生图失败会自动回到稳定路径。",
  },
  {
    value: "dual_race",
    label: "双并发",
    description: "Codex 原生和 image2 直连同时跑，先完成的结果返回。速度更激进，但会消耗双倍配额。",
    badge: "配额翻倍",
  },
];

const IMAGE_CHANNEL_OPTIONS: readonly SettingChoice[] = [
  {
    value: "auto",
    label: "自动混合",
    description: "按选中的 Provider 能力分发：支持异步任务走 image-job，不支持则走流式。",
  },
  {
    value: "stream_only",
    label: "强制流式",
    description: "所有 Provider 都走 responses 或 image2 直连，不使用异步任务服务。",
  },
  {
    value: "image_jobs_only",
    label: "强制异步",
    description: "只允许支持 image-job 的 Provider；选中不支持的 Provider 会直接返回 503。",
    badge: "严格",
  },
];

const IMAGE_OUTPUT_FORMAT_OPTIONS: readonly SettingChoice[] = [
  {
    value: "jpeg",
    label: "JPEG",
    description: "默认选项。文件小，适合分享。",
  },
  {
    value: "png",
    label: "PNG",
    description: "文件更大，适合保存透明背景或继续编辑。",
  },
];

const SETTING_META: Record<string, SettingMeta> = {
  [SITE_PUBLIC_BASE_URL_KEY]: {
    group: "site",
    title: "站点域名",
    summary: "生成邀请链接和分享链接时使用的对外访问地址。",
    detail:
      "填写 web 根地址，例如 https://your-domain.example.com。不要带 /api、/invite 或其它路径；留空时后端会按当前访问域名自动生成。",
    kind: "url",
    icon: Globe,
    recommended: "生产环境建议显式填写真实 HTTPS 域名。",
    keywords: ["site", "public", "base", "url", "domain", "域名", "邀请链接", "分享链接"],
  },
  [SITE_SHARE_EXPIRATION_DAYS_KEY]: {
    group: "site",
    title: "分享链接有效期",
    summary: "新生成图片分享链接默认多久后失效。",
    detail: "只影响保存后新生成的分享链接；已经生成的旧链接保持原来的过期时间。设为 0 表示永久有效。",
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
    summary: "决定图片生成使用 Codex 原生、image2 直连还是双路竞速。",
    detail: "不确定时选“Codex 原生”。双并发会同时消耗两条路径的配额，默认收起。",
    kind: "enum",
    icon: ImageIcon,
    defaultValue: "responses",
    recommended: "默认：Codex 原生",
    choices: IMAGE_ENGINE_OPTIONS,
    keywords: ["image", "engine", "responses", "image2", "dual"],
  },
  "image.channel": {
    group: "image",
    title: "异步通道",
    summary: "控制是否把支持 image-job 的 Provider 分发到异步任务通道。",
    detail: "auto 会按 Provider 能力混合分发；stream_only 完全关闭异步任务；image_jobs_only 会严格要求 Provider 支持异步任务。",
    kind: "enum",
    icon: Activity,
    defaultValue: "auto",
    recommended: "默认：自动混合",
    choices: IMAGE_CHANNEL_OPTIONS,
    keywords: ["image", "channel", "image_jobs", "stream", "auto", "异步"],
  },
  [IMAGE_OUTPUT_FORMAT_KEY]: {
    group: "image",
    title: "输出格式",
    summary: "设置新生成图片默认使用 JPEG 还是 PNG。",
    detail:
      "JPEG 体积更小；PNG 更接近无损画质但文件更大。透明背景请求始终使用 PNG，不受这里影响。",
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
    detail: "默认 https://image-job.example.com。可填写服务根地址，也可填写以 /v1 结尾的地址。",
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
    detail: "手动选择 4K 或固定尺寸时，不受这个值限制。",
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
  "upstream.global_concurrency": {
    group: "upstream",
    title: "同时请求上游的数量",
    summary: "控制全站最多同时向上游发多少个请求。",
    detail: "调太高可能触发上游限流；调太低会让排队变长。",
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
    detail: "网络正常时不用改；如果经常连接超时，可以适当加大。",
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
    detail: "4K 图片和复杂任务可能更慢，需要给足时间。",
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
    summary: "定时用一道简单算术题检查 Provider 是否可用。",
    detail: "设为 0 表示关闭自动探活，只保留手动探活。",
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
    detail: "每次都会消耗一次图片配额。生产环境建议关闭，或至少 30 分钟以上。",
    kind: "integer",
    icon: ImageIcon,
    unit: "秒",
    min: 0,
    max: 86400,
    defaultValue: "0",
    recommended: "默认：0，先关闭。",
    warning: "会消耗上游图片配额。",
    keywords: ["provider", "image", "probe", "图片探活"],
  },
  [UPDATE_USE_PROXY_POOL_KEY]: {
    group: "update",
    title: "更新时使用代理池",
    summary: "一键更新 Lumen 时，让 git、uv 和 npm 的出站请求走代理池。",
    detail: "关闭时直接更新；开启后会使用下面选中的代理。这个设置只影响管理后台触发的一键更新。",
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
    detail: "留空时后端会使用代理池中第一个启用代理。代理列表在“代理池”标签页维护。",
    kind: "text",
    icon: Rocket,
    recommended: "优先选择已测试成功、延迟稳定的代理。",
    keywords: ["update", "proxy", "name", "更新代理"],
  },
  "context.compression_enabled": {
    group: "context_auto",
    title: "自动压缩长对话",
    summary: "对话快超过上下文时，自动把较早内容整理成摘要。",
    detail: "长对话会更稳，但摘要质量取决于所选模型。",
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
    detail: "数值越低，越早压缩；数值越高，越接近上限才压缩。",
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
    detail: "越大越完整，但会占用更多上下文；越小越省空间，但信息可能损失。",
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
    detail: "保留越多，当前话题越不容易断；但会占用更多上下文。",
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
    detail: "用来避免对话在阈值附近反复压缩。",
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
    detail: "超过上限时，系统会分段汇总，避免单次请求过大。",
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
    detail: "暂停期间系统会改用保守截断，避免连续失败影响对话。",
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
    label: "Provider 探活",
    description: "自动检测账号可用性",
    icon: Activity,
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

const SETTINGS_SKELETON_KEYS = [
  "settings-skeleton-summary",
  "settings-skeleton-image",
  "settings-skeleton-context",
] as const;

export function SettingsPanel() {
  const q = useSystemSettingsQuery();
  const updateMut = useUpdateSystemSettingsMutation();
  const adminModelsQ = useAdminModelsQuery({ retry: false });
  const providersQ = useProvidersQuery({ retry: false });
  const proxiesQ = useAdminProxiesQuery({ retry: false });
  // SSE 已连接时关闭 status 轮询，避免 5s GET 整体覆盖 SSE 增量造成 checklist 闪烁。
  // streamConnectedRef 由下方 useEffect 与 useAdminUpdateStream 的 streamStatus 同步。
  const streamConnectedRef = useRef(false);
  const updateStatusQ = useAdminUpdateStatusQuery({
    retry: false,
    refetchInterval: (query) => {
      if (streamConnectedRef.current) return false;
      return query.state.data?.running ? 5000 : false;
    },
  });
  const releasesQ = useAdminReleasesQuery({ retry: false });
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
  const [updateBanner, setUpdateBanner] = useState<{
    kind: "success" | "error" | "info";
    text: string;
  } | null>(null);
  const triggerUpdateMut = useTriggerAdminUpdateMutation({
    onSuccess: (result) => {
      const target = result.unit ? `任务 ${result.unit}` : `进程 ${result.pid ?? "-"}`;
      setUpdateBanner({
        kind: "success",
        text: `更新已启动，${target}${result.proxy_name ? `，代理 ${result.proxy_name}` : ""}`,
      });
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.message : err.message || "触发更新失败";
      setUpdateBanner({ kind: "error", text: `触发更新失败：${msg}` });
    },
  });
  const rollbackMut = useRollbackReleaseMutation({
    onSuccess: (result) => {
      setUpdateBanner({
        kind: "success",
        text: `回滚已启动，目标 release ${result.target.id}`,
      });
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.message : err.message || "触发回滚失败";
      setUpdateBanner({ kind: "error", text: `触发回滚失败：${msg}` });
    },
  });

  // SSE 实时流：跑动时打开，连接 /admin/update/stream
  // 该 hook 直接管理 status query data + 本地 log buffer（done 事件触发 invalidate）。
  // running 端为 true → 开流；false 时不打开（避免没必要的连接）。
  // 用户主动触发更新/回滚后，前端把 banner 设了，但 status 还是旧的（异步轮询），
  // 所以 enable 条件除了 running 还看 mutation pending，确保点击后立刻开流。
  const sseEnabled =
    Boolean(updateStatusQ.data?.running) ||
    triggerUpdateMut.isPending ||
    rollbackMut.isPending;
  const { logBuffer, streamStatus, clearLogs } = useAdminUpdateStream(sseEnabled);

  // 把 streamStatus 桥接到 streamConnected，给 useAdminUpdateStatusQuery 的
  // refetchInterval 用。connecting / broken / error 状态都视为"未连接"，让轮询兜底。
  useEffect(() => {
    streamConnectedRef.current = streamStatus === "open";
  }, [streamStatus]);

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
          ? `${jobs} / ${total} 个 Provider 已启用异步任务`
          : "未配置 Provider",
      compact:
        providersQ.isLoading || total === 0 ? "auto" : `auto · ${jobs}/${total} 启用`,
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
          errors[key] = "请填写一个数值";
          continue;
        }
        const n = Number(raw);
        if (!Number.isFinite(n)) {
          errors[key] = "请填写有效数字";
          continue;
        }
        if (meta.kind === "integer" && !Number.isInteger(n)) {
          errors[key] = "请填写整数，不要带小数";
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
          errors[key] = "请选择开启或关闭";
          continue;
        }
        payload.push({ key, value: raw });
        continue;
      }

      if (meta.kind === "enum") {
        if (!meta.choices?.some((option) => option.value === raw)) {
          errors[key] = "请选择一个有效选项";
          continue;
        }
        payload.push({ key, value: raw });
        continue;
      }

      if (meta.kind === "url") {
        const normalized = normalizePublicBaseUrlInput(raw);
        if (!normalized) {
          errors[key] = "请填写完整的 http(s) 根域名，不要带路径、参数或 /api";
          continue;
        }
        payload.push({ key, value: normalized });
        continue;
      }

      if (raw === "") {
        errors[key] = "不能为空";
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
      setGlobalError("还有设置没有填对，请先修正红色提示。");
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
    <section className="space-y-5 pb-24">
      <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/70 p-4 shadow-[var(--shadow-2)] backdrop-blur-sm md:p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex min-w-0 items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-[var(--color-lumen-amber)]/25 bg-[var(--color-lumen-amber)]/12">
              <SlidersHorizontal className="h-4 w-4 text-[var(--color-lumen-amber)]" />
            </div>
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-[var(--fg-0)]">
                系统设置
              </h2>
              <p className="mt-1 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
                这些设置会影响图片生成、上游请求和长对话处理。保存后通常几秒内对 API 和 Worker 生效。
              </p>
            </div>
          </div>
          <div className="inline-flex w-fit items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs text-neutral-300">
            <Database className="h-3.5 w-3.5 text-neutral-500" />
            数据库设置优先生效
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

      <AnimatePresence>
        {normalizeImageEngine(imageEngine) === "dual_race" && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className="flex items-start gap-2 rounded-xl border border-red-500/35 bg-red-500/8 px-4 py-3 text-sm text-red-200"
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
            className="flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/5 px-4 py-3 text-sm text-red-300"
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
            className="flex items-center gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-4 py-3 text-sm text-emerald-300"
          >
            <Check className="h-4 w-4" /> 已保存
          </motion.div>
        )}
      </AnimatePresence>

      <ContextHealthBlock
        loading={contextHealthQ.isLoading}
        error={contextHealthQ.error}
        onRetry={() => void contextHealthQ.refetch()}
        data={contextHealthQ.data}
      />

      <LumenUpdateBlock
        status={updateStatusQ.data}
        loading={updateStatusQ.isLoading}
        error={updateStatusQ.error}
        triggering={triggerUpdateMut.isPending}
        banner={updateBanner}
        releases={releasesQ.data}
        releasesLoading={releasesQ.isLoading}
        releasesError={releasesQ.error}
        rollbackPendingId={
          rollbackMut.isPending ? rollbackMut.variables ?? null : null
        }
        logBuffer={logBuffer}
        streamStatus={streamStatus}
        onTrigger={() => {
          setUpdateBanner(null);
          clearLogs();
          triggerUpdateMut.mutate();
        }}
        onRefresh={() => {
          void updateStatusQ.refetch();
          void releasesQ.refetch();
        }}
        onRollback={(releaseId) => {
          setUpdateBanner(null);
          clearLogs();
          rollbackMut.mutate(releaseId);
        }}
        onClearBanner={() => setUpdateBanner(null)}
      />

      <div className="space-y-3">
        <div className="-mx-4 overflow-x-auto px-4 md:mx-0 md:px-0">
          <div className="inline-flex min-w-max items-center gap-1 rounded-full border border-white/10 bg-white/[0.04] p-1">
            {GROUPS.map((group) => {
              const active = activeGroup === group.id;
              const Icon = group.icon;
              const count =
                group.id === "all" ? visibleItems.length : groupCounts[group.id] ?? 0;
              if (group.id !== "all" && count === 0) return null;
              return (
                <button
                  key={group.id}
                  type="button"
                  onClick={() => setActiveGroup(group.id)}
                  className={cn(
                    "inline-flex min-h-[36px] cursor-pointer items-center gap-1.5 rounded-full px-3 text-xs transition-colors",
                    active
                      ? "bg-[var(--color-lumen-amber)] text-black"
                      : "text-neutral-400 hover:bg-white/8 hover:text-neutral-200",
                  )}
                  title={group.description}
                >
                  <Icon className="h-3.5 w-3.5" />
                  <span>{group.label}</span>
                  <span
                    className={cn(
                      "rounded-full px-1.5 py-0.5 font-mono text-[10px]",
                      active ? "bg-black/10" : "bg-white/8 text-neutral-500",
                    )}
                  >
                    {count}
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <label className="relative block md:w-80">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-neutral-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索设置、说明或技术名"
              className="h-10 w-full rounded-xl border border-white/10 bg-black/25 pl-9 pr-3 text-sm text-neutral-100 outline-none transition-colors placeholder:text-neutral-600 focus:border-[var(--color-lumen-amber)]/55 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/20"
            />
          </label>
          <p className="text-xs text-neutral-500">
            当前显示 {visibleCount} 项，已修改 {dirtyCount} 项
          </p>
        </div>
      </div>

      {q.isLoading ? (
        <div className="space-y-3">
          {SETTINGS_SKELETON_KEYS.map((key, i) => (
            <div
              key={key}
              className="h-32 animate-pulse rounded-2xl bg-white/5"
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
        <div className="flex flex-col items-center gap-3 rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 py-14 text-center text-sm text-neutral-500 backdrop-blur-sm">
          <Sparkles className="h-5 w-5 text-neutral-600" />
          没有可配置项
        </div>
      ) : visibleCount === 0 ? (
        <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 px-4 py-12 text-center text-sm text-neutral-500">
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

      <AnimatePresence>
        {dirtyCount > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 30 }}
            transition={{ duration: 0.2 }}
            className="fixed bottom-0 left-0 right-0 z-40 max-w-full px-4 pb-[env(safe-area-inset-bottom)] sm:bottom-4 sm:left-1/2 sm:right-auto sm:w-auto sm:max-w-[calc(100vw-2rem)] sm:-translate-x-1/2 sm:px-0 sm:pb-4"
          >
            <div className="flex items-center gap-2 rounded-2xl border border-[var(--color-lumen-amber)]/40 bg-[var(--bg-1)]/95 px-3 py-2.5 shadow-[0_20px_60px_-20px_rgba(0,0,0,0.8)] backdrop-blur-xl sm:gap-3 sm:px-4">
              <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs text-neutral-300">
                <span className="h-1.5 w-1.5 rounded-full bg-[var(--color-lumen-amber)] shadow-[0_0_8px_currentColor]" />
                <span className="font-mono tabular-nums">{dirtyCount}</span>
                <span>项待保存</span>
              </span>
              <div className="flex-1 sm:flex-none" />
              <button
                type="button"
                onClick={onResetAll}
                disabled={updateMut.isPending}
                className="inline-flex min-h-[40px] cursor-pointer items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 text-xs text-neutral-300 transition-colors hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50 sm:h-8"
              >
                <RotateCcw className="h-3 w-3" /> 放弃
              </button>
              <button
                type="button"
                onClick={onSave}
                disabled={updateMut.isPending}
                className="inline-flex min-h-[40px] cursor-pointer items-center gap-1.5 rounded-lg bg-[var(--color-lumen-amber)] px-4 text-xs font-medium text-black transition-[filter,transform] hover:brightness-110 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-50 sm:h-8"
              >
                {updateMut.isPending ? (
                  <>
                    <Loader2 className="h-3 w-3 animate-spin" /> 保存中
                  </>
                ) : (
                  <>
                    <Save className="h-3 w-3" /> 保存全部
                  </>
                )}
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
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
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 bg-white/[0.04]">
            <Icon className="h-4 w-4 text-neutral-400" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-medium text-neutral-100">
              {group.label}
            </h3>
            <p className="mt-0.5 text-xs text-neutral-500">
              {group.description}
            </p>
          </div>
        </div>
        <span className="shrink-0 rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 font-mono text-[11px] text-neutral-500">
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
        "rounded-xl border p-3 backdrop-blur-sm transition-colors md:p-4",
        isDirty
          ? "border-[var(--color-lumen-amber)]/45 bg-[var(--color-lumen-amber)]/[0.05] shadow-[0_10px_30px_-15px_var(--color-lumen-amber)]"
          : "border-white/10 bg-[var(--bg-1)]/60",
      )}
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 bg-white/[0.04]">
            <Icon className="h-4 w-4 text-[var(--color-lumen-amber)]" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h4 className="text-sm font-medium text-neutral-100">
                {meta.title}
              </h4>
              <span className="rounded-md border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[11px] text-neutral-300">
                当前：{displayValue}
              </span>
              <SourceBadge
                hasDbOverride={hasDbOverride}
                hasAnyValue={item.has_value}
              />
            </div>
            <p className="mt-1 text-sm leading-6 text-neutral-400">
              {meta.summary}
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => setShowDetails((value) => !value)}
          className="inline-flex min-h-[32px] w-fit cursor-pointer items-center gap-1 rounded-lg border border-white/10 bg-white/5 px-2 text-xs text-neutral-300 transition-colors hover:bg-white/10"
        >
          {showDetails ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
          详情
        </button>
      </div>

      {meta.warning && (
        <div className="mt-3 flex items-start gap-2 rounded-lg border border-amber-500/25 bg-amber-500/8 px-3 py-2 text-xs leading-5 text-amber-200">
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

      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-neutral-500">
        {meta.recommended && (
          <span className="rounded-md border border-emerald-500/20 bg-emerald-500/8 px-2 py-1 text-emerald-300/90">
            {meta.recommended}
          </span>
        )}
        {(meta.min != null || meta.max != null) && (
          <span className="rounded-md border border-white/10 bg-white/[0.03] px-2 py-1">
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
            <div className="mt-3 space-y-2 rounded-lg border border-white/10 bg-black/18 px-3 py-2 text-xs leading-5 text-neutral-500">
              {meta.detail && <p>{meta.detail}</p>}
              <p>
                技术名{" "}
                <code className="font-mono text-neutral-400">{item.key}</code>
              </p>
              {item.description && item.description !== meta.summary && (
                <p>{item.description}</p>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {fieldError && (
        <p className="mt-3 flex items-center gap-1.5 text-xs text-red-300">
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
                  "min-h-[72px] cursor-pointer rounded-lg border px-3 py-2 text-left transition-colors",
                  option.value === "dual_race"
                    ? "border-red-500/35 bg-red-500/8"
                    : selected
                      ? "border-[var(--color-lumen-amber)]/60 bg-[var(--color-lumen-amber)]/10 text-neutral-100"
                      : "border-white/10 bg-black/20 text-neutral-300 hover:bg-white/5",
                )}
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium">{option.label}</span>
                  {option.badge && (
                    <span
                      className={cn(
                        "rounded-full border px-2 py-0.5 text-[10px]",
                        option.value === "dual_race"
                          ? "border-red-500/35 bg-red-500/10 text-red-200"
                          : "border-amber-500/25 bg-amber-500/10 text-amber-200",
                      )}
                    >
                      {option.badge}
                    </span>
                  )}
                </span>
                <span className="mt-1 block text-xs leading-5 text-neutral-500">
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
            className="inline-flex min-h-[32px] cursor-pointer items-center gap-1 rounded-lg border border-red-500/25 bg-red-500/5 px-2 text-xs text-red-200 transition-colors hover:bg-red-500/10"
          >
            <ChevronRight className="h-3.5 w-3.5" />
            显示进阶路径
          </button>
        )}
        {item.key === IMAGE_CHANNEL_KEY && (
          <p className="text-xs text-neutral-500">{providerStatus.label}</p>
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

  if (item.key === UPDATE_PROXY_NAME_KEY) {
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
            "relative h-8 w-14 shrink-0 cursor-pointer rounded-full border transition-colors focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/30",
            checked
              ? "border-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)]"
              : "border-white/15 bg-white/10",
          )}
        >
          <span
            className={cn(
              "absolute top-1 h-6 w-6 rounded-full bg-white shadow-sm transition-transform",
              checked ? "translate-x-7" : "translate-x-1",
            )}
          />
        </button>
        <span
          className={cn(
            "inline-flex rounded-md border px-2 py-1 text-xs",
            checked
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
              : "border-white/10 bg-white/5 text-neutral-400",
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
            className="h-11 w-full rounded-xl border border-white/10 bg-black/30 px-3 pr-16 font-mono text-sm text-neutral-100 outline-none transition-colors placeholder:text-neutral-600 focus:border-[var(--color-lumen-amber)]/55 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/20"
          />
          {meta.unit && (
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-neutral-500">
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
        className="h-11 w-full flex-1 rounded-xl border border-white/10 bg-black/30 px-3 font-mono text-sm text-neutral-100 outline-none transition-colors placeholder:text-neutral-600 focus:border-[var(--color-lumen-amber)]/55 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/20"
      />
      {meta.kind === "url" && browserOrigin && (
        <button
          type="button"
          onClick={() => onChange({ kind: "set", value: browserOrigin })}
          className="inline-flex min-h-[40px] cursor-pointer items-center justify-center gap-1.5 rounded-xl border border-white/10 bg-white/5 px-3 text-xs text-neutral-300 transition-colors hover:bg-white/10 md:h-11"
        >
          <Globe className="h-3.5 w-3.5" />
          填入当前域名
        </button>
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
        <p className="text-xs text-amber-200">
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
          className="h-11 flex-1 rounded-xl border border-white/10 bg-black/30 px-3 font-mono text-sm text-neutral-100 outline-none transition-colors focus:border-[var(--color-lumen-amber)]/55 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/20"
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
        <button
          type="button"
          onClick={() => setCustomMode(false)}
          className="inline-flex min-h-[40px] cursor-pointer items-center justify-center rounded-xl border border-white/10 bg-white/5 px-3 text-xs text-neutral-300 transition-colors hover:bg-white/10 md:h-11"
        >
          返回列表
        </button>
      )}
      <ResetEditButton
        dirty={!!op}
        defaultValue={meta.defaultValue}
        showDefaultAction={showDefaultAction}
        onReset={() => onChange(undefined)}
        onUseDefault={(defaultValue) => onChange({ kind: "set", value: defaultValue })}
      />
      {modelsQuery.isLoading && (
        <span className="inline-flex items-center gap-1 text-xs text-neutral-500">
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
          className="h-11 flex-1 rounded-xl border border-white/10 bg-black/30 px-3 text-sm text-neutral-100 outline-none transition-colors focus:border-[var(--color-lumen-amber)]/55 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/20"
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
        <p className="text-xs text-amber-200">
          代理池没有启用代理；开启“更新时使用代理池”后，一键更新会被后端拒绝。
        </p>
      ) : (
        <p className="text-xs text-neutral-500">
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
        className="h-11 flex-1 rounded-xl border border-white/10 bg-black/30 px-3 font-mono text-sm text-neutral-100 outline-none transition-colors placeholder:text-neutral-600 focus:border-[var(--color-lumen-amber)]/55 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/20"
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
      <button
        type="button"
        onClick={onReset}
        className="inline-flex min-h-[40px] cursor-pointer items-center justify-center gap-1.5 rounded-xl border border-white/10 bg-white/5 px-3 text-xs text-neutral-300 transition-colors hover:bg-white/10 md:h-11"
      >
        <RotateCcw className="h-3.5 w-3.5" />
        撤销修改
      </button>
    );
  }
  if (!defaultValue || !showDefaultAction) return null;
  return (
    <button
      type="button"
      onClick={() => onUseDefault(defaultValue)}
      className="inline-flex min-h-[40px] cursor-pointer items-center justify-center gap-1.5 rounded-xl border border-white/10 bg-white/5 px-3 text-xs text-neutral-300 transition-colors hover:bg-white/10 md:h-11"
    >
      <Check className="h-3.5 w-3.5" />
      填入默认值
    </button>
  );
}

// ——— Lumen 一键更新：phase 中文映射 + 默认顺序 ———
// 后端契约里 phase 是开放枚举；前端列出"标准 12 phase + rollback"做骨架。
// 顺序对齐 update.sh 实际执行顺序；rollback 默认隐藏，只在 status.phases 出现时显示。
const PHASE_ORDER: readonly string[] = [
  "prepare",
  "fetch",
  "link_shared",
  "containers",
  "deps_python",
  "migrate_db",
  "deps_node",
  "build_web",
  "switch",
  "restart",
  "health_post",
  "cleanup",
];

const PHASE_LABEL: Record<string, string> = {
  prepare: "准备",
  fetch: "拉代码",
  link_shared: "链接共享",
  containers: "容器",
  deps_python: "Python 依赖",
  migrate_db: "数据库迁移",
  deps_node: "Node 依赖",
  build_web: "前端构建",
  switch: "原子切换",
  restart: "重启服务",
  health_post: "健康检查",
  cleanup: "清理旧版本",
  rollback: "回滚",
};

function phaseLabel(phase: string): string {
  return PHASE_LABEL[phase] ?? phase;
}

function formatDuration(ms: number | null | undefined): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const totalSec = ms / 1000;
  if (totalSec < 60) return `${totalSec.toFixed(1)}s`;
  const m = Math.floor(totalSec / 60);
  const s = Math.round(totalSec - m * 60);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

function shortReleaseId(id: string): string {
  if (id.length <= 28) return id;
  return id.slice(0, 28) + "…";
}

function shortSha(sha?: string | null): string {
  if (!sha) return "未知";
  return sha.length > 7 ? sha.slice(0, 7) : sha;
}

// ——— SSE 实时流 hook ———
//
// 设计：
//  - enabled=true 时打开 EventSource；enabled→false 立即关闭（避免漏关）
//  - state 事件 → 整体替换 status query 缓存（snapshot 同步）
//  - step 事件 → 局部合并 phases（按 phase 名匹配；不存在则 append）
//  - log 事件 → push 到 ringbuffer（最多 500 行）
//  - done → 关闭流 + invalidate（拿一次最新 status / releases）
//  - error → 自动重连，指数退避 1/2/5/15s，最多 5 次后停止显示"连接中断"
//  - URL 用同源 /api/admin/update/stream（apiClient.adminUpdateStreamUrl）
//
// 注意：EventSource 命名事件必须用 addEventListener；onmessage 只接默认 message。
const LOG_BUFFER_MAX = 500;
const SSE_RETRY_DELAYS_MS = [1000, 2000, 5000, 15000, 15000];
const SSE_MAX_RETRIES = SSE_RETRY_DELAYS_MS.length;

type AdminStreamStatus = "idle" | "connecting" | "open" | "error" | "broken";

interface AdminUpdateStreamHandle {
  logBuffer: string[];
  streamStatus: AdminStreamStatus;
  clearLogs: () => void;
}

function useAdminUpdateStream(enabled: boolean): AdminUpdateStreamHandle {
  // QueryClient 实例在 Provider 之下保持稳定，不需要 ref 包装。
  const qc = useQueryClient();
  const [logBuffer, setLogBuffer] = useState<string[]>([]);
  const [streamStatus, setStreamStatus] = useState<AdminStreamStatus>("idle");

  // qc 在 effect 内被 closure 捕获；同时同步到 ref 必须在 effect 内（React 19
  // refs lint 不允许 render 期间写 ref.current）。
  const qcRef = useRef(qc);
  useEffect(() => {
    qcRef.current = qc;
  });

  const clearLogs = useCallback(() => {
    setLogBuffer([]);
  }, []);

  useEffect(() => {
    if (!enabled) {
      // 异步置 idle，避免 effect 同步 setState 级联（react-hooks/set-state-in-effect）
      const t = setTimeout(() => setStreamStatus("idle"), 0);
      return () => clearTimeout(t);
    }
    if (typeof window === "undefined" || typeof EventSource === "undefined") {
      const t = setTimeout(() => setStreamStatus("idle"), 0);
      return () => clearTimeout(t);
    }

    let es: EventSource | null = null;
    let retryAttempt = 0;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    const clearRetry = () => {
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    const close = () => {
      clearRetry();
      if (es) {
        try {
          es.close();
        } catch {
          /* ignore */
        }
        es = null;
      }
    };

    const mergeStep = (step: UpdateStepRecord) => {
      qcRef.current.setQueryData<AdminUpdateStatusOut | undefined>(
        qk.adminUpdateStatus(),
        (prev) => {
          if (!prev) {
            return {
              running: step.status === "running",
              log_tail: "",
              phases: [step],
            };
          }
          const phases = prev.phases ? [...prev.phases] : [];
          const idx = phases.findIndex((p) => p.phase === step.phase);
          if (idx >= 0) {
            phases[idx] = { ...phases[idx], ...step };
          } else {
            phases.push(step);
          }
          return { ...prev, phases };
        },
      );
    };

    const mergeInfo = (payload: { phase: string; key: string; value: string }) => {
      qcRef.current.setQueryData<AdminUpdateStatusOut | undefined>(
        qk.adminUpdateStatus(),
        (prev) => {
          if (!prev) return prev;
          const phases = prev.phases ? [...prev.phases] : [];
          const idx = phases.findIndex((p) => p.phase === payload.phase);
          if (idx < 0) return prev;
          const cur = phases[idx];
          phases[idx] = {
            ...cur,
            info: { ...(cur.info ?? {}), [payload.key]: payload.value },
          };
          return { ...prev, phases };
        },
      );
    };

    const open = () => {
      if (disposed) return;
      close();
      setStreamStatus("connecting");
      try {
        es = new EventSource(adminUpdateStreamUrl(), { withCredentials: true });
      } catch {
        setStreamStatus("error");
        scheduleRetry();
        return;
      }

      es.onopen = () => {
        retryAttempt = 0;
        setStreamStatus("open");
      };

      const parseData = <T,>(raw: string): T | null => {
        try {
          return JSON.parse(raw) as T;
        } catch {
          return null;
        }
      };

      es.addEventListener("state", (ev: MessageEvent) => {
        const snapshot = parseData<AdminUpdateStatusOut>(ev.data);
        if (!snapshot) return;
        qcRef.current.setQueryData(qk.adminUpdateStatus(), snapshot);
        if (snapshot.releases) {
          qcRef.current.setQueryData(qk.adminReleases(), snapshot.releases);
        }
      });

      es.addEventListener("step", (ev: MessageEvent) => {
        const step = parseData<UpdateStepRecord>(ev.data);
        if (!step || !step.phase) return;
        mergeStep(step);
      });

      es.addEventListener("info", (ev: MessageEvent) => {
        const info = parseData<{ phase: string; key: string; value: string }>(
          ev.data,
        );
        if (!info || !info.phase || !info.key) return;
        mergeInfo(info);
      });

      es.addEventListener("log", (ev: MessageEvent) => {
        const payload = parseData<{ line?: string }>(ev.data);
        if (!payload || typeof payload.line !== "string") return;
        const line = payload.line;
        setLogBuffer((prev) => {
          const next =
            prev.length >= LOG_BUFFER_MAX
              ? prev.slice(-(LOG_BUFFER_MAX - 1))
              : prev.slice();
          next.push(line);
          return next;
        });
      });

      es.addEventListener("done", (ev: MessageEvent) => {
        const payload = parseData<{ final_status?: AdminUpdateStatusOut }>(
          ev.data,
        );
        if (payload?.final_status) {
          qcRef.current.setQueryData(
            qk.adminUpdateStatus(),
            payload.final_status,
          );
        }
        qcRef.current.invalidateQueries({ queryKey: qk.adminUpdateStatus() });
        qcRef.current.invalidateQueries({ queryKey: qk.adminReleases() });
        close();
        setStreamStatus("idle");
      });

      es.onerror = () => {
        setStreamStatus("error");
        close();
        scheduleRetry();
      };
    };

    const scheduleRetry = () => {
      if (disposed) return;
      clearRetry();
      if (retryAttempt >= SSE_MAX_RETRIES) {
        setStreamStatus("broken");
        return;
      }
      const delay = SSE_RETRY_DELAYS_MS[retryAttempt] ?? 15000;
      retryAttempt += 1;
      retryTimer = setTimeout(() => {
        if (!disposed) open();
      }, delay);
    };

    open();

    return () => {
      disposed = true;
      close();
    };
  }, [enabled]);

  return { logBuffer, streamStatus, clearLogs };
}

// ——— 主组件 ———

interface LumenUpdateBlockProps {
  status: AdminUpdateStatusOut | undefined;
  loading: boolean;
  error: Error | null;
  triggering: boolean;
  banner: { kind: "success" | "error" | "info"; text: string } | null;
  releases: ReleaseInfo[] | undefined;
  releasesLoading: boolean;
  releasesError: Error | null;
  rollbackPendingId: string | null;
  logBuffer: string[];
  streamStatus: AdminStreamStatus;
  onTrigger: () => void;
  onRefresh: () => void;
  onRollback: (releaseId: string) => void;
  onClearBanner: () => void;
}

function LumenUpdateBlock({
  status,
  loading,
  error,
  triggering,
  banner,
  releases,
  releasesLoading,
  releasesError,
  rollbackPendingId,
  logBuffer,
  streamStatus,
  onTrigger,
  onRefresh,
  onRollback,
  onClearBanner,
}: LumenUpdateBlockProps) {
  const running = Boolean(status?.running);
  const isRollingBack = rollbackPendingId != null;
  const disabled = triggering || running || isRollingBack;
  const runningTarget = status?.unit
    ? `unit ${status.unit}`
    : `pid ${status?.pid ?? "-"}`;
  const phases = useMemo(() => status?.phases ?? [], [status?.phases]);
  const phaseByName = useMemo(() => {
    const m = new Map<string, UpdateStepRecord>();
    for (const p of phases) m.set(p.phase, p);
    return m;
  }, [phases]);

  // checklist 显示策略：默认 12 标准 phase；如出现非标准 phase（如 rollback）追加在末尾
  const checklist = useMemo<string[]>(() => {
    const order = [...PHASE_ORDER];
    const seen = new Set(order);
    for (const p of phases) {
      if (!seen.has(p.phase)) {
        order.push(p.phase);
        seen.add(p.phase);
      }
    }
    return order;
  }, [phases]);

  const failed = useMemo(
    () => phases.some((p) => p.status === "done" && p.rc != null && p.rc !== 0),
    [phases],
  );

  const [logOpen, setLogOpen] = useState(false);
  const logRef = useRef<HTMLPreElement | null>(null);
  const userScrolledRef = useRef(false);
  useEffect(() => {
    if (!logOpen) return;
    const el = logRef.current;
    if (!el) return;
    if (!userScrolledRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [logOpen, logBuffer]);
  const onLogScroll: React.UIEventHandler<HTMLPreElement> = (e) => {
    const el = e.currentTarget;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    userScrolledRef.current = distanceFromBottom > 16;
  };

  const [pendingRollback, setPendingRollback] = useState<ReleaseInfo | null>(null);

  // banner 自动消失：success / info 6s 后清；error 不动
  useEffect(() => {
    if (!banner) return;
    if (banner.kind === "error") return;
    const t = setTimeout(() => onClearBanner(), 6000);
    return () => clearTimeout(t);
  }, [banner, onClearBanner]);

  const effectiveBanner =
    banner ??
    (failed && !running
      ? {
          kind: "error" as const,
          text: "上次更新失败，请查看 checklist 中的红色 phase 或日志。",
        }
      : null);

  return (
    <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 p-4 backdrop-blur-sm">
      {/* —— 顶部状态条 —— */}
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-[var(--color-lumen-amber)]/25 bg-[var(--color-lumen-amber)]/12">
            <Rocket className="h-4 w-4 text-[var(--color-lumen-amber)]" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-medium text-neutral-100">
              一键更新 Lumen
            </h3>
            <p className="mt-1 text-xs leading-5 text-neutral-500">
              后台执行更新脚本，失败可在下方 release 历史里回滚到旧版本。
            </p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            className="inline-flex min-h-[40px] cursor-pointer items-center justify-center gap-1.5 rounded-xl border border-white/10 bg-white/5 px-3 text-xs text-neutral-300 transition-colors hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50 md:h-10"
          >
            {loading ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RotateCcw className="h-3.5 w-3.5" />
            )}
            刷新状态
          </button>
          <button
            type="button"
            onClick={onTrigger}
            disabled={disabled}
            className="inline-flex min-h-[40px] cursor-pointer items-center justify-center gap-1.5 rounded-xl bg-[var(--color-lumen-amber)] px-4 text-xs font-medium text-black transition-[filter,transform] hover:brightness-110 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-50 md:h-10"
          >
            {triggering || running ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> 更新中
              </>
            ) : (
              <>
                <Rocket className="h-3.5 w-3.5" /> 一键更新
              </>
            )}
          </button>
        </div>
      </div>

      {/* —— 状态徽章 —— */}
      <div className="mt-4 flex flex-wrap gap-2 text-xs">
        <span
          className={cn(
            "rounded-md border px-2 py-1",
            running
              ? isRollingBack
                ? "border-amber-500/25 bg-amber-500/10 text-amber-200"
                : "border-sky-500/25 bg-sky-500/10 text-sky-200"
              : failed
                ? "border-red-500/25 bg-red-500/10 text-red-200"
                : "border-emerald-500/25 bg-emerald-500/10 text-emerald-300",
          )}
        >
          {running
            ? `${isRollingBack ? "回滚运行中" : "更新运行中"} · ${runningTarget}`
            : failed
              ? "上次任务失败"
              : phases.length > 0
                ? "上次任务完成"
                : "当前没有更新任务"}
        </span>
        {status?.started_at && (
          <span className="rounded-md border border-white/10 bg-white/[0.04] px-2 py-1 text-neutral-400">
            启动时间 {formatDateTime(status.started_at)}
          </span>
        )}
        {running && (
          <span
            className={cn(
              "rounded-md border px-2 py-1",
              streamStatus === "open"
                ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-300"
                : streamStatus === "connecting"
                  ? "border-sky-500/25 bg-sky-500/10 text-sky-200"
                  : streamStatus === "broken"
                    ? "border-red-500/25 bg-red-500/10 text-red-200"
                    : "border-white/10 bg-white/[0.04] text-neutral-400",
            )}
          >
            实时流：
            {streamStatus === "open"
              ? "已连接"
              : streamStatus === "connecting"
                ? "连接中"
                : streamStatus === "broken"
                  ? "中断，请刷新"
                  : streamStatus === "error"
                    ? "重连中"
                    : "未连接"}
          </span>
        )}
      </div>

      {error && (
        <p className="mt-3 text-xs text-red-300">
          更新状态读取失败：{error.message}
        </p>
      )}

      {effectiveBanner && (
        <div
          className={cn(
            "mt-3 flex items-start justify-between gap-3 rounded-xl border px-3 py-2 text-sm",
            effectiveBanner.kind === "success"
              ? "border-emerald-500/30 bg-emerald-500/8 text-emerald-200"
              : effectiveBanner.kind === "error"
                ? "border-red-500/30 bg-red-500/8 text-red-200"
                : "border-sky-500/30 bg-sky-500/8 text-sky-200",
          )}
        >
          <span className="min-w-0 break-words">{effectiveBanner.text}</span>
          <button
            type="button"
            onClick={onClearBanner}
            className="shrink-0 rounded p-0.5 hover:bg-white/8"
            aria-label="关闭提示"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {/* —— Step Checklist —— */}
      <div className="mt-4 rounded-xl border border-white/10 bg-black/20">
        <div className="flex items-center justify-between border-b border-white/5 px-3 py-2">
          <span className="text-xs font-medium text-neutral-300">执行步骤</span>
          {phases.length > 0 && (
            <span className="text-[11px] text-neutral-500">
              {phases.filter((p) => p.status === "done" && (p.rc ?? 0) === 0).length} /{" "}
              {checklist.length} 完成
            </span>
          )}
        </div>
        <ol className="divide-y divide-white/5">
          {checklist.map((phase) => (
            <PhaseRow key={phase} phase={phase} record={phaseByName.get(phase)} />
          ))}
        </ol>
      </div>

      {/* —— 实时 Log —— */}
      <div className="mt-3">
        <button
          type="button"
          onClick={() => setLogOpen((v) => !v)}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-white/10 bg-white/[0.04] px-2.5 py-1 text-xs text-neutral-300 transition-colors hover:bg-white/8"
        >
          <Terminal className="h-3.5 w-3.5" />
          {logOpen ? "收起实时输出" : "查看实时输出"}
          {logBuffer.length > 0 && (
            <span className="rounded-full bg-white/8 px-1.5 py-0.5 font-mono text-[10px] text-neutral-400">
              {logBuffer.length}
            </span>
          )}
        </button>
        <AnimatePresence initial={false}>
          {logOpen && (
            <motion.div
              key="log-panel"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.18 }}
              className="overflow-hidden"
            >
              <pre
                ref={logRef}
                onScroll={onLogScroll}
                className="mt-2 max-h-72 overflow-auto rounded-xl border border-white/10 bg-black/40 p-3 font-mono text-[11px] leading-5 text-neutral-300"
              >
                {logBuffer.length > 0
                  ? logBuffer.join("\n")
                  : status?.log_tail
                    ? status.log_tail
                    : "（暂无输出）"}
              </pre>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* —— Release 历史 —— */}
      <div className="mt-5 rounded-xl border border-white/10 bg-black/20">
        <div className="flex items-center gap-2 border-b border-white/5 px-3 py-2">
          <History className="h-3.5 w-3.5 text-neutral-400" />
          <span className="text-xs font-medium text-neutral-300">Release 历史</span>
          <span className="text-[11px] text-neutral-500">最近 10 个版本</span>
        </div>
        {releasesError ? (
          <p className="px-3 py-3 text-xs text-red-300">
            读取 release 列表失败：{releasesError.message}
          </p>
        ) : releasesLoading && !releases ? (
          <div className="space-y-1.5 p-3">
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                className="h-10 animate-pulse rounded-md bg-white/5"
                style={{ animationDelay: `${i * 60}ms` }}
              />
            ))}
          </div>
        ) : !releases || releases.length === 0 ? (
          <p className="px-3 py-3 text-xs text-neutral-500">暂无 release 记录。</p>
        ) : (
          <ul className="divide-y divide-white/5">
            {releases.map((r) => (
              <ReleaseRow
                key={r.id}
                release={r}
                rollingBack={rollbackPendingId === r.id}
                disabled={disabled}
                onRollback={() => setPendingRollback(r)}
              />
            ))}
          </ul>
        )}
      </div>

      <ConfirmDialog
        open={pendingRollback != null}
        onOpenChange={(open) => {
          if (!open) setPendingRollback(null);
        }}
        title="回滚到此版本？"
        description={
          pendingRollback
            ? `回滚到 release ${pendingRollback.id}？将切回旧代码并重启 Lumen 服务（约 30 秒不可用）。数据库不会回滚，仅切代码。`
            : ""
        }
        confirmText="确认回滚"
        cancelText="取消"
        tone="danger"
        confirming={isRollingBack}
        onConfirm={() => {
          if (!pendingRollback) return;
          const id = pendingRollback.id;
          setPendingRollback(null);
          onRollback(id);
        }}
      />
    </div>
  );
}

// ——— 同文件内子组件 ———

function PhaseRow({
  phase,
  record,
}: {
  phase: string;
  record: UpdateStepRecord | undefined;
}) {
  const status = record?.status;
  const rc = record?.rc;
  const isDone = status === "done";
  const isRunning = status === "running";
  const isFailed = isDone && rc != null && rc !== 0;
  const isOk = isDone && (rc == null || rc === 0);
  const dur = formatDuration(record?.dur_ms);
  const infoEntries = record?.info ? Object.entries(record.info) : [];

  return (
    <li className="flex items-start gap-3 px-3 py-2">
      <span
        className={cn(
          "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[10px]",
          isRunning
            ? "border-sky-400/40 bg-sky-500/15 text-sky-200"
            : isOk
              ? "border-emerald-500/30 bg-emerald-500/15 text-emerald-300"
              : isFailed
                ? "border-red-500/40 bg-red-500/15 text-red-300"
                : "border-white/15 bg-white/[0.03] text-neutral-500",
        )}
        aria-hidden="true"
      >
        {isRunning ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : isOk ? (
          <Check className="h-3 w-3" />
        ) : isFailed ? (
          <X className="h-3 w-3" />
        ) : (
          <Circle className="h-2 w-2" />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span
            className={cn(
              "text-xs",
              isRunning
                ? "text-sky-200"
                : isFailed
                  ? "text-red-200"
                  : isOk
                    ? "text-neutral-200"
                    : "text-neutral-500",
            )}
          >
            {phaseLabel(phase)}
          </span>
          <span className="font-mono text-[10px] text-neutral-600">{phase}</span>
          {isFailed && rc != null && (
            <span className="rounded-md border border-red-500/30 bg-red-500/10 px-1.5 py-0.5 font-mono text-[10px] text-red-300">
              rc={rc}
            </span>
          )}
        </div>
        {infoEntries.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-neutral-500">
            {infoEntries.map(([k, v]) => (
              <span key={k} className="font-mono">
                {k}={v}
              </span>
            ))}
          </div>
        )}
      </div>
      {dur && (
        <span className="ml-2 shrink-0 self-center text-[11px] tabular-nums text-neutral-500">
          {dur}
        </span>
      )}
    </li>
  );
}

function ReleaseRow({
  release,
  rollingBack,
  disabled,
  onRollback,
}: {
  release: ReleaseInfo;
  rollingBack: boolean;
  disabled: boolean;
  onRollback: () => void;
}) {
  const alembic = release.alembic_head_applied || release.alembic_head_expected;
  const showRollback = !release.is_current;
  return (
    <li className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center sm:gap-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="font-mono text-xs text-neutral-200" title={release.id}>
            {shortReleaseId(release.id)}
          </span>
          {release.is_current && (
            <span className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] text-emerald-300">
              当前
            </span>
          )}
          {release.is_previous && !release.is_current && (
            <span className="rounded-md border border-white/10 bg-white/[0.04] px-1.5 py-0.5 text-[10px] text-neutral-400">
              上一个
            </span>
          )}
        </div>
        <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-neutral-500">
          <span>{formatDateTime(release.created_at)}</span>
          <span className="font-mono" title={release.sha ?? undefined}>
            sha {shortSha(release.sha)}
          </span>
          {release.branch && <span>分支 {release.branch}</span>}
          {alembic && (
            <span className="font-mono" title={alembic}>
              alembic {alembic.slice(0, 12)}
            </span>
          )}
        </div>
      </div>
      {showRollback && (
        <button
          type="button"
          onClick={onRollback}
          disabled={disabled}
          className="inline-flex min-h-[34px] shrink-0 cursor-pointer items-center justify-center gap-1.5 self-start rounded-md border border-white/10 bg-white/5 px-2.5 text-[11px] text-neutral-300 transition-colors hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50 sm:self-center"
        >
          {rollingBack ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin" /> 回滚中
            </>
          ) : (
            <>
              <Undo2 className="h-3 w-3" /> 回滚到此版本
            </>
          )}
        </button>
      )}
    </li>
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
    <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 p-4 backdrop-blur-sm">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-white/10 bg-white/[0.04]">
            <ShieldCheck className="h-4 w-4 text-neutral-400" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-medium text-neutral-100">
              长对话摘要状态
            </h3>
            <p className="mt-1 text-xs leading-5 text-neutral-500">
              用来判断自动摘要是否稳定。这里是只读状态，不需要手动保存。
            </p>
          </div>
        </div>
        {loading ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-neutral-400">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> 读取中
          </span>
        ) : error ? (
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex min-h-[36px] cursor-pointer items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 text-xs text-neutral-300 transition-colors hover:bg-white/10"
          >
            <RotateCcw className="h-3 w-3" /> 重试
          </button>
        ) : (
          <span
            className={cn(
              "inline-flex items-center rounded-md border px-2 py-0.5 text-xs",
              state.tone === "danger"
                ? "border-red-500/30 bg-red-500/10 text-red-300"
                : state.tone === "warning"
                  ? "border-amber-500/30 bg-amber-500/10 text-amber-200"
                  : "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
            )}
          >
            {state.label}
          </span>
        )}
      </div>

      {error ? (
        <p className="mt-3 text-xs text-neutral-500">
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
        <p className="mt-3 text-xs text-amber-300">
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
    <div className="rounded-xl border border-white/10 bg-black/18 px-3 py-2.5">
      <div className="flex items-center gap-2 text-[11px] text-neutral-500">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <p className="mt-1 truncate text-sm font-medium text-neutral-100">
        {value}
      </p>
    </div>
  );
}

function HealthMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-white/8 bg-black/20 px-3 py-2">
      <p className="text-[11px] text-neutral-500">{label}</p>
      <p className="mt-1 font-mono text-sm text-neutral-100">{value}</p>
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
    <div className="flex items-start gap-3 rounded-xl border border-white/10 bg-white/[0.04] px-3 py-3 text-sm text-neutral-300">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 bg-black/20">
        <Icon className="h-4 w-4 text-neutral-400" />
      </div>
      <div>
        <p className="font-medium text-neutral-100">{title}</p>
        <p className="mt-1 text-xs leading-5 text-neutral-500">{body}</p>
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
      <span className="rounded-md border border-[var(--color-lumen-amber)]/25 bg-[var(--color-lumen-amber)]/10 px-2 py-0.5 text-[11px] text-[var(--color-lumen-amber)]">
        已覆盖默认
      </span>
    );
  }
  if (hasAnyValue) {
    return (
      <span className="rounded-md border border-sky-500/20 bg-sky-500/8 px-2 py-0.5 text-[11px] text-sky-300">
        使用环境变量
      </span>
    );
  }
  return (
    <span className="rounded-md border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[11px] text-neutral-500">
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
  return {
    group:
      prefix === "site"
        ? "site"
        : prefix === "image"
        ? "image"
        : prefix === "upstream"
          ? "upstream"
          : prefix === "providers"
            ? "providers"
            : prefix === "update"
              ? "update"
            : prefix === "context"
              ? "context_auto"
              : "advanced",
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
    "Codex 原生"
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
    "JPEG"
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
