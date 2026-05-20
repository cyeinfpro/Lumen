// Lightbox 内部共享类型（CustomEvent 契约 + panel 展示用）。

export type LightboxParamBag = Record<string, unknown>;

export interface LightboxProviderAttempt {
  provider?: string | null;
  route?: string | null;
  endpoint?: string | null;
  proxy?: string | null;
  status?: string | null;
  duration_ms?: number | null;
  error_summary?: string | null;
}

export interface LightboxItem {
  id: string;
  /** 下载 / 外链查看用的原始图 URL（通常是 /api/images/{id}/binary）。 */
  url: string;
  /**
   * 展示层优先使用的预览 URL（推荐 display2048 variant）。
   * 解码 4K 原图会阻塞 → 必须传 previewUrl 才不会卡。
   */
  previewUrl?: string;
  /** 缩略图条使用的小图 URL。 */
  thumbUrl?: string;
  prompt?: string;
  width?: number;
  height?: number;
  aspect_ratio?: string;
  size_actual?: string;
  size_requested?: string;
  seed?: string | number;
  quality?: string;
  render_quality?: string;
  output_format?: string;
  output_compression?: number | string | null;
  background?: string;
  moderation?: string;
  fast?: boolean;
  /** 生成模型名或模型 id，按调用方已有数据透传展示。 */
  model?: string;
  model_id?: string;
  /** 文件 MIME 类型；兼容不同后端命名。 */
  mime?: string;
  mime_type?: string;
  content_type?: string;
  /** 宽泛的资源类型，例如 image/png、png、generated-image。 */
  type?: string;
  /** 原始文件名；用于后续下载文件名推断。 */
  filename?: string;
  file_name?: string;
  created_at?: string;
  updated_at?: string;
  /** 上游返回的改写提示词；兼容未来后端直接透传。 */
  revised_prompt?: string | null;
  /** 用户请求参数与实际生效参数；也可能藏在 metadata/upstream_request 里。 */
  params?: LightboxParamBag | null;
  image_params?: LightboxParamBag | null;
  requested_params?: LightboxParamBag | null;
  request_params?: LightboxParamBag | null;
  effective_params?: LightboxParamBag | null;
  actual_params?: LightboxParamBag | null;
  diagnostics?: LightboxParamBag | null;
  /** 基础版本 / 来源链路字段；旧数据缺失时静默隐藏。 */
  source?: string | null;
  source_type?: string | null;
  source_id?: string | null;
  parent_image_id?: string | null;
  parent_generation_id?: string | null;
  from_generation_id?: string | null;
  generation_id?: string | null;
  message_id?: string | null;
  conversation_id?: string | null;
  action_source?: string | null;
  generation_action?: string | null;
  /** 上游运行痕迹；字段都是可选的，旧数据不返回时静默隐藏。 */
  provider?: string | null;
  upstream_provider?: string | null;
  actual_provider?: string | null;
  initial_provider?: string | null;
  first_provider?: string | null;
  proxy_name?: string | null;
  proxy_enabled?: boolean | null;
  duration_ms?: number | null;
  upstream_duration_ms?: number | null;
  upstream_duration_seconds?: number | null;
  elapsed_ms?: number | null;
  failover?: boolean | null;
  provider_failover?: boolean | null;
  failover_count?: number | null;
  debug_id?: string | null;
  trace_id?: string | null;
  request_id?: string | null;
  provider_attempts?: LightboxProviderAttempt[];
  safe_error_summary?: string | null;
  upstream_error_summary?: string | null;
  error_summary?: string | null;
  metadata?: Record<string, unknown>;
}

export interface OpenLightboxDetail {
  items: LightboxItem[];
  initialId: string;
  fromRect?: DOMRect;
  /**
   * 派发来源标记。`store` 表示由 `useUiStore.openLightboxFromItems` 在写入 store
   * 后镜像派发（仅供 MobileLightbox 监听）；DesktopLightbox 收到此 flag 时应跳过
   * 二次写库以保留 action / eventItems。
   */
  source?: "store" | "external";
}

export const OPEN_EVENT = "lumen:open-lightbox";
export const CLOSE_EVENT = "lumen:close-lightbox";

/** 解析 aspect_ratio 字符串（"16:9" / "1:1"）为 width/height 比值。 */
export function parseAspectRatio(
  item: LightboxItem | null | undefined,
): number | null {
  if (!item) return null;
  if (item.width && item.height && item.height > 0) {
    return item.width / item.height;
  }
  const ar = item.aspect_ratio;
  if (ar && /^\d+\s*:\s*\d+$/.test(ar)) {
    const [w, h] = ar.split(":").map((s) => Number(s.trim()));
    if (w > 0 && h > 0) return w / h;
  }
  return null;
}
