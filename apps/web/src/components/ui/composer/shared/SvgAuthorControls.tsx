"use client";

import { VisionSvgControls } from "./VisionSvgControls";
import { Select } from "@/components/ui/primitives";
import { SVG_REASONING_EFFORTS } from "@/lib/svgAuthor";
import type { SvgReasoningEffort } from "@/lib/types";
import { useChatStore } from "@/store/useChatStore";

export function SvgAuthorControls() {
  const svg = useChatStore((state) => state.composer.params.svg);
  const setSvg = useChatStore((state) => state.setImageSvgAuthor);
  return (
    <div className="grid gap-2">
      <label className="grid gap-1 type-caption">
        SVG 作者/绘制模型 ID
        <input aria-label="SVG 作者/绘制模型 ID" value={svg?.model ?? ""}
          placeholder="留空不指定，部分通道会拒绝"
          onChange={(event) => setSvg({ ...svg, model: event.target.value })}
          className="min-h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 text-[var(--fg-0)]" />
      </label>
      <label className="grid gap-1 type-caption">
        SVG 作者推理强度
        <Select aria-label="SVG 作者推理强度" value={svg?.reasoning?.effort ?? ""}
          disabled={!svg?.model.trim()}
          onChange={(event) => setSvg({ model: svg?.model ?? "", ...(event.target.value ? { reasoning: { effort: event.target.value as SvgReasoningEffort } } : {}) })}>
          <option value="">默认（不指定）</option>
          {SVG_REASONING_EFFORTS.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
        </Select>
      </label>
      <p className="type-caption text-[var(--fg-2)]">
        仅当使用 https://flux.infpro.me/v1 时，可参考 2026-11-05 实测作者 gpt-6.1-sol。此提示不代表当前路由或可用性保证；其他通道需另行确认。不会自动填写或覆盖您的选择。
      </p>
      <p className="type-caption text-[var(--fg-2)]">
        作者模型独立于 Flare/Sunburst 参考图模型及复杂度，并会独立计费。请填写通道支持的模型 ID；留空不发送 SVG 作者配置。原生 SVG 仅支持文生图，不支持参考图编辑。
      </p>
      <VisionSvgControls />
    </div>
  );
}
