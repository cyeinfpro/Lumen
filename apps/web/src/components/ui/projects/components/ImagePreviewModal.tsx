"use client";

// Deprecated 包装：保留旧调用方契约（images / index / onClose / onIndexChange），
// 内部转发给全局 Lightbox（useUiStore.openLightboxFromItems）。
//
// 全局 Lightbox（apps/web/src/components/ui/lightbox）已支持手势缩放、左右翻页、
// 下载、移动/桌面分流；本组件不再自行渲染 overlay，仅在 index 变成 valid 时
// 触发一次 store 打开，并立即把父级 index 重置回 -1，避免双源不一致。
//
// 后续清理：调用方可直接换成 useUiStore.getState().openLightboxFromItems(items, id)，
// 然后移除该文件。

import { useEffect, useRef } from "react";

import type { BackendImageMeta } from "@/lib/apiClient";
import type { LightboxItem } from "@/components/ui/lightbox/types";
import { useUiStore } from "@/store/useUiStore";

interface ImagePreviewModalProps {
  images: BackendImageMeta[];
  index: number;
  onClose: () => void;
  /** 旧契约保留：父组件设置 index 用；当全局 Lightbox 打开后立即被回拨到 -1。 */
  onIndexChange?: (next: number) => void;
}

function toLightboxItem(image: BackendImageMeta): LightboxItem {
  return {
    id: image.id,
    url: image.url,
    previewUrl: image.display_url ?? image.preview_url ?? undefined,
    thumbUrl: image.thumb_url ?? undefined,
    width: image.width,
    height: image.height,
    mime: image.mime,
  };
}

export function ImagePreviewModal({
  images,
  index,
  onClose,
}: ImagePreviewModalProps) {
  // 仅当 index 从 -1/越界 变成有效值时触发一次；用 ref 防止 effect 重入。
  const dispatchedRef = useRef<string | null>(null);
  useEffect(() => {
    const valid = images.length > 0 && index >= 0 && index < images.length;
    if (!valid) {
      dispatchedRef.current = null;
      return;
    }
    const target = images[index];
    const key = `${target.id}#${index}`;
    if (dispatchedRef.current === key) return;
    dispatchedRef.current = key;
    const items = images.map(toLightboxItem);
    useUiStore.getState().openLightboxFromItems(items, target.id);
    // 把父级 index 重置回 -1，全局 Lightbox 接管后续状态；避免双源。
    onClose();
  }, [images, index, onClose]);

  return null;
}

export default ImagePreviewModal;
