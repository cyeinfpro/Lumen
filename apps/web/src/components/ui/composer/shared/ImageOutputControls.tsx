"use client";

import { Select } from "@/components/ui/primitives";
import type { ImageOutputFormat } from "@/lib/types";
import { useChatStore } from "@/store/useChatStore";

export function ImageOutputControls() {
  const params = useChatStore((state) => state.composer.params);
  const setFormat = useChatStore((state) => state.setImageOutputFormat);
  const setFrames = useChatStore((state) => state.setImageFrameCount);
  const setDuration = useChatStore((state) => state.setImageFrameDuration);
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
      {params.output_format === "gif" && (
        <div className="grid grid-cols-2 gap-2">
          <label className="grid gap-1 type-caption">
            动画帧数
            <input aria-label="动画帧数" type="number" min={2} max={16} step={1}
              value={params.frame_count ?? 9}
              onChange={(event) => setFrames(Number(event.target.value))}
              className="min-h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 text-[var(--fg-0)]" />
          </label>
          <label className="grid gap-1 type-caption">
            每帧时长（毫秒）
            <input aria-label="每帧时长" type="number" min={20} max={2000} step={10}
              value={params.frame_duration_ms ?? 200}
              onChange={(event) => setDuration(Number(event.target.value))}
              className="min-h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 text-[var(--fg-0)]" />
          </label>
        </div>
      )}
      {special && (
        <p className="type-caption text-[var(--fg-2)]">
          支持文生图或静态 PNG/JPEG/WebP 参考图，每次 1 个结果；暂不开放蒙版或标注组合。
          {params.output_format === "gif" ? " 图生 GIF 需供应商开通 GIF 预览权限，未开通时显示上游错误。默认每帧 512×512，下载保留完整动画。" : " 图生 SVG 为供应商兼容性试验，尚未验证正式请求契约；不保证成功或无损矢量化。下载保留真实 SVG。"}
        </p>
      )}
    </section>
  );
}
