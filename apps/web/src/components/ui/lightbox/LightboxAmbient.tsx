"use client";

// LightboxAmbient —— 环境光 halo。
// 按 spec §4.10：绝对定位 inset:-20%，图自身当背景、blur 80px、saturate 1.4、
// brightness 0.38；CSS 已在 globals.css 的 `.lumen-ambient` 定义好。
// 我们只负责注入 `--bg-img` 并做透明度动画。

import { motion, type MotionValue } from "framer-motion";
import type { CSSProperties } from "react";

export interface LightboxAmbientProps {
  /** 当前图 URL；为空时只渲染一个纯暗底 */
  imageUrl: string | null;
  /** 由手势层驱动的透明度（下拉逐渐淡出） */
  opacity: MotionValue<number>;
}

export function LightboxAmbient({ imageUrl, opacity }: LightboxAmbientProps) {
  const style: CSSProperties = imageUrl
    ? ({ ["--bg-img" as string]: `url("${imageUrl.replace(/"/g, '\\"')}")` } as CSSProperties)
    : {};
  return (
    <motion.div
      aria-hidden
      className="lumen-ambient"
      style={{ ...style, opacity }}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.32, ease: [0.16, 1, 0.3, 1] }}
    />
  );
}
