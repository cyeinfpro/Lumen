"use client";

// Lightbox 入口 —— 按 viewport 分流：
// - < 768px → MobileLightbox（监听 lumen:open-lightbox 事件，URL ?img 深链）
// - ≥ 768px → DesktopLightbox（继续走 useUiStore 的原行为）
// 首次 SSR / 未定维度时返回 null（Lightbox 是叠加层，不渲染骨架）。

import { useIsMobile } from "@/hooks/useMediaQuery";
import { DesktopLightbox } from "./DesktopLightbox";
import { MobileLightbox } from "./MobileLightbox";

export function Lightbox() {
  const isMobile = useIsMobile();
  if (isMobile === null) return null;
  return isMobile ? <MobileLightbox /> : <DesktopLightbox />;
}
