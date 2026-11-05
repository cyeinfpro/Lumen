"use client";

import { GifSizeControls } from "./GifSizeControls";
import { GifTimingControls } from "./GifTimingControls";
import { SvgAuthorControls } from "./SvgAuthorControls";
import { Select } from "@/components/ui/primitives";
import type { ImageOutputFormat } from "@/lib/types";
import { useChatStore } from "@/store/useChatStore";

export function ImageOutputControls() {
  const params = useChatStore((state) => state.composer.params);
  const setFormat = useChatStore((state) => state.setImageOutputFormat);
  const special = params.output_format === "svg" || params.output_format === "gif";
  return (
    <section className="grid gap-2 text-[var(--fg-0)]">
      <label className="grid gap-1">
        <span className="type-overline text-[var(--fg-2)]">输出格式</span>
        <Select
          aria-label="输出格式"
          value={params.output_format ?? "png"}
          onChange={(event) => setFormat(event.target.value as ImageOutputFormat)}
        >
          <option value="png">PNG</option>
          <option value="jpeg">JPEG</option>
          <option value="webp">WebP</option>
          <option value="svg">SVG 矢量图</option>
          <option value="gif">GIF 动画</option>
        </Select>
      </label>
      {params.output_format === "svg" && <SvgAuthorControls />}
      {params.output_format === "gif" && <GifSizeControls />}
      {params.output_format === "gif" && <GifTimingControls />}
      {special && (
        <p className="type-caption text-[var(--fg-2)]">
          {params.output_format === "gif" ? "支持文生 GIF 或静态 PNG/JPEG/WebP 参考图；图生 GIF 需通道开通相应权限，未开通时显示上游错误。下载保留完整动画。" : "原生 SVG 仅支持文生图，不支持参考图编辑。请移除参考图或改用 PNG/WebP/GIF；不会静默切换到视觉模型重绘。每次 1 个结果，下载保留真实 SVG。"}
          暂不开放蒙版或标注组合。
        </p>
      )}
    </section>
  );
}
