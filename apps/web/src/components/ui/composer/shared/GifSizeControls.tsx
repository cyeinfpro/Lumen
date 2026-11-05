"use client";

import { Select } from "@/components/ui/primitives";
import { GIF_SIZE_PRESETS, gifPresetId, gifSizeError } from "@/lib/gifSize";
import { useChatStore } from "@/store/useChatStore";

export function GifSizeControls() {
  const params = useChatStore((state) => state.composer.params);
  const setMode = useChatStore((state) => state.setSizeMode);
  const setSize = useChatStore((state) => state.setFixedSize);
  const setFrames = useChatStore((state) => state.setImageFrameCount);
  const [width = "512", height = "512"] = (params.fixed_size ?? "512x512").split("x");
  const error = gifSizeError(params);
  return (
    <div className="grid gap-2">
      <label className="grid gap-1 type-caption">
        尺寸 / 帧数组合预设
        <Select aria-label="GIF 尺寸帧数预设" value={gifPresetId(params)}
          onChange={(event) => {
            const preset = GIF_SIZE_PRESETS.find((item) => item.id === event.target.value);
            if (!preset) return;
            setMode("fixed");
            setSize(preset.size);
            setFrames(preset.frames);
          }}>
          <option value="">自定义 / 自动</option>
          {GIF_SIZE_PRESETS.map((preset) => <option key={preset.id} value={preset.id}>
            {preset.size.replace("x", "×")} · {preset.frames} 帧
          </option>)}
        </Select>
      </label>
      <p className="type-caption text-[var(--fg-2)]">选择预设会同时修改宽高和帧数，保留每帧时长。组合在 flux.infpro.me/v1 于 2026-11-05 实测通过，不保证其他通道或后续请求成功。</p>
      <label className="grid gap-1 type-caption">
        GIF 每帧尺寸
        <Select aria-label="GIF 每帧尺寸" value={params.size_mode}
          onChange={(event) => setMode(event.target.value as "auto" | "fixed")}>
          <option value="fixed">自定义宽高</option>
          <option value="auto">自动</option>
        </Select>
      </label>
      {params.size_mode !== "auto" && <div className="grid grid-cols-2 gap-2">
        {(["宽", "高"] as const).map((label, index) => (
          <label key={label} className="grid gap-1 type-caption">
            GIF {label}（像素）
            <input aria-label={`GIF ${label}`} type="number" min={16} step={16}
              value={index === 0 ? width : height}
              onChange={(event) => setSize(index === 0 ? `${event.target.value}x${height}` : `${width}x${event.target.value}`)}
              className="min-h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 text-[var(--fg-0)]" />
          </label>
        ))}
      </div>}
      <p className="type-caption text-[var(--fg-2)]">正整数且 16 对齐仅是基础检查，不代表上游接受。内部帧拼图与请求总预算未知；若超限，可改用预设或自动尺寸，参考通道错误调整。这里请求原生尺寸，不自动放大；4K 静态图支持不代表原生 4K GIF 可用。</p>
      {error && <p role="alert" className="type-caption text-[var(--danger-fg)]">{error}</p>}
    </div>
  );
}
