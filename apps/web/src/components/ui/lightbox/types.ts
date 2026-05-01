// Lightbox 内部共享类型（CustomEvent 契约 + panel 展示用）。

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
  seed?: string | number;
  quality?: string;
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
  metadata?: Record<string, unknown>;
}

export interface OpenLightboxDetail {
  items: LightboxItem[];
  initialId: string;
  fromRect?: DOMRect;
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
