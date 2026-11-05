"use client";

import { gifTimingError, gifTimingPreview } from "@/lib/gifSize";
import { useChatStore } from "@/store/useChatStore";

export function GifTimingControls() {
  const params = useChatStore((state) => state.composer.params);
  const setFrames = useChatStore((state) => state.setImageFrameCount);
  const setDuration = useChatStore((state) => state.setImageFrameDuration);
  const timing = gifTimingPreview(params);
  const error = gifTimingError(params);
  return (
    <div className="grid gap-2">
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
      {timing && <p role="status" aria-label="GIF 计时预览" className="type-caption text-[var(--fg-1)]">
        请求编码帧率 {Number(timing.fps.toFixed(2))} FPS · 每轮 {Number(timing.loopSeconds.toFixed(3))} 秒
      </p>}
      <p className="type-caption text-[var(--fg-2)]">
        每次 1 个结果、2–16 帧、20–2000 ms 是 Lumen 的保守产品范围，不代表所有通道的硬限制。时长使用 10 ms 整数倍，避免编码量化；最终以文件计时为准，播放器实际帧率可能更低。16 帧 × 20 ms 只有 0.32 秒。
      </p>
      {error && <p role="alert" className="type-caption text-[var(--danger-fg)]">{error}</p>}
    </div>
  );
}
