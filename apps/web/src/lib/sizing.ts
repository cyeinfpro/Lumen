// DESIGN.md §7.2 + 附录 A：尺寸解析器
// 目标：根据比例 / 模式 / 可选固定尺寸，产出最终 `size` 字段 + 可能追加到 prompt 末尾的比例强指令。
//
// 两层策略（4K 升级后）：
// - 默认（auto / fixed=空）：沿用保守的 ~1.57M `PIXEL_BUDGET` 推导 preset，控制延迟与成本
// - 显式 fixed_size：按上游 gpt-image-2 真实能力校验（16 对齐 / 最长边 ≤3840 /
//   总像素 ∈ [655360, 8294400] / 长宽比 ≤3:1）；非法时 throw Error 而非静默回退
//
// 与 packages/core/lumen_core/sizing.py 完全对称。

import type {
  AspectRatio,
  ImageOutputFormat,
  Quality,
  RenderQuality,
  ResolvedSize,
  SizeMode,
} from "./types";

// size_mode=auto 下的默认像素预算（preset / fallback 推导用）。
export const PIXEL_BUDGET = 1_572_864;

// 显式 fixed_size 的合法边界（对应 gpt-image-2 的真实能力；2026-04-23 已实测 3840x2160 可用）。
export const MAX_EXPLICIT_SIDE = 3840;
export const MIN_EXPLICIT_PIXELS = 655_360;
export const MAX_EXPLICIT_PIXELS = 8_294_400; // = 3840 * 2160
export const MAX_EXPLICIT_ASPECT = 3.0;
export const EXPLICIT_ALIGN = 16;

// 4K 快捷预设——UI 直接用这组字面量，保持前后端校验不变。
export const PRESET_4K_LANDSCAPE = "3840x2160" as const;
export const PRESET_4K_PORTRAIT = "2160x3840" as const;

const RATIO_MAP: Record<AspectRatio, { w: number; h: number }> = {
  "1:1": { w: 1, h: 1 },
  "16:9": { w: 16, h: 9 },
  "9:16": { w: 9, h: 16 },
  "21:9": { w: 21, h: 9 },
  "9:21": { w: 9, h: 21 },
  "4:5": { w: 4, h: 5 },
  "3:4": { w: 3, h: 4 },
  "4:3": { w: 4, h: 3 },
  "3:2": { w: 3, h: 2 },
  "2:3": { w: 2, h: 3 },
};

// 默认 preset：按"默认最大 4K 画质 + 按比例分配"的策略升级。
// 每条均满足 validateExplicitSize：16 对齐、最长边 ≤ 3840、总像素 ≤ 8,294,400、长宽比 ≤ 3:1。
// 横/竖构图配对：3:2↔2:3 / 4:3↔3:4 / 16:9↔9:16 / 21:9↔9:21
export const PRESET: Record<AspectRatio, { w: number; h: number }> = {
  "1:1":  { w: 2880, h: 2880 }, // 8,294,400
  "16:9": { w: 3840, h: 2160 }, // 8,294,400
  "9:16": { w: 2160, h: 3840 },
  "21:9": { w: 3808, h: 1632 }, // 6,214,656
  "9:21": { w: 1632, h: 3808 },
  "4:5":  { w: 2560, h: 3200 }, // 8,192,000
  "3:4":  { w: 2448, h: 3264 }, // 7,989,072
  "4:3":  { w: 3264, h: 2448 },
  "3:2":  { w: 3504, h: 2336 }, // 8,185,344
  "2:3":  { w: 2336, h: 3504 },
};

export const PRESET_1K: Record<AspectRatio, { w: number; h: number }> = {
  "1:1":  { w: 1024, h: 1024 },
  "16:9": { w: 1536, h: 864 },
  "9:16": { w: 864, h: 1536 },
  "21:9": { w: 1536, h: 656 },
  "9:21": { w: 656, h: 1536 },
  "4:5":  { w: 1024, h: 1280 },
  "3:4":  { w: 1024, h: 1360 },
  "4:3":  { w: 1360, h: 1024 },
  "3:2":  { w: 1440, h: 960 },
  "2:3":  { w: 960, h: 1440 },
};

export const PRESET_2K: Record<AspectRatio, { w: number; h: number }> = {
  "1:1":  { w: 1440, h: 1440 },
  "16:9": { w: 2048, h: 1152 },
  "9:16": { w: 1152, h: 2048 },
  "21:9": { w: 2240, h: 960 },
  "9:21": { w: 960, h: 2240 },
  "4:5":  { w: 1280, h: 1600 },
  "3:4":  { w: 1248, h: 1664 },
  "4:3":  { w: 1664, h: 1248 },
  "3:2":  { w: 1872, h: 1248 },
  "2:3":  { w: 1248, h: 1872 },
};

export function qualityToFixedSize(quality: Quality, aspect: AspectRatio): { size_mode: "auto" | "fixed"; fixed_size?: string } {
  if (quality === "1k") {
    const p = PRESET_1K[aspect] ?? PRESET_1K["1:1"];
    return { size_mode: "fixed", fixed_size: `${p.w}x${p.h}` };
  }
  if (quality === "4k") {
    const p = PRESET[aspect] ?? PRESET["16:9"];
    return { size_mode: "fixed", fixed_size: `${p.w}x${p.h}` };
  }
  // 2k default
  const p = PRESET_2K[aspect] ?? PRESET_2K["1:1"];
  return { size_mode: "fixed", fixed_size: `${p.w}x${p.h}` };
}

export function defaultOutputCompression(input: {
  renderQuality: RenderQuality;
  outputFormat: ImageOutputFormat;
  fast: boolean;
}): number | undefined {
  void input.renderQuality;
  void input.fast;
  if (input.outputFormat === "png") return undefined;
  return 0;
}

export function ratioInstruction(aspect: AspectRatio): string {
  // guide 对图生图尤其重要：prompt 末尾明示比例能把 `size=auto` 的实际输出拉到目标比例附近。
  return ` Preserve a strict ${aspect} composition.`;
}

/**
 * 把 "W:H" 字符串转 CSS aspect-ratio（"W / H"），无效时回退 "4 / 3"。
 * Canonical 实现：跨端（components/desktop / mobile / DevelopingCard）从此处 import，
 * 避免 3 处重复定义。
 */
export function aspectRatioToCss(ratio: string | null | undefined): string {
  if (!ratio) return "4 / 3";
  const [w, h] = ratio.split(":").map((x) => Number.parseFloat(x));
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) {
    return "4 / 3";
  }
  return `${w} / ${h}`;
}

/** 校验 fixed_size 是否符合上游真实能力；返回 null=合法，返回 string=原因（本地化中文）。 */
export function validateExplicitSize(
  w: number,
  h: number,
): string | null {
  if (!Number.isInteger(w) || !Number.isInteger(h) || w <= 0 || h <= 0) {
    return `尺寸必须是正整数（收到 ${w}×${h}）`;
  }
  if (w % EXPLICIT_ALIGN !== 0 || h % EXPLICIT_ALIGN !== 0) {
    return `宽高必须是 ${EXPLICIT_ALIGN} 的倍数（收到 ${w}×${h}）`;
  }
  const longest = Math.max(w, h);
  if (longest > MAX_EXPLICIT_SIDE) {
    return `最长边不得超过 ${MAX_EXPLICIT_SIDE}（收到 ${longest}）`;
  }
  const px = w * h;
  if (px < MIN_EXPLICIT_PIXELS || px > MAX_EXPLICIT_PIXELS) {
    return `总像素需在 [${MIN_EXPLICIT_PIXELS}, ${MAX_EXPLICIT_PIXELS}] 区间（收到 ${px}）`;
  }
  const ratio = longest / Math.min(w, h);
  if (ratio > MAX_EXPLICIT_ASPECT) {
    return `长宽比不得超过 ${MAX_EXPLICIT_ASPECT}:1（收到 ${ratio.toFixed(3)}）`;
  }
  return null;
}

/** 解析 "WxH" 字符串；非法时返回 null。 */
export function parseFixedSize(
  s: string,
): { w: number; h: number } | null {
  if (!/^\d+x\d+$/.test(s)) return null;
  const [w, h] = s.split("x").map((v) => parseInt(v, 10));
  if (!Number.isFinite(w) || !Number.isFinite(h)) return null;
  return { w, h };
}

export function resolveSize(input: {
  aspect: AspectRatio;
  mode: SizeMode;
  fixed?: string;
}): ResolvedSize {
  const { aspect, mode, fixed } = input;

  if (mode === "auto") {
    return { size: "auto", prompt_suffix: ratioInstruction(aspect) };
  }

  // fixed 模式：显式尺寸按上游真实能力校验，允许 4K 等大图直通
  if (fixed) {
    const parsed = parseFixedSize(fixed);
    if (!parsed) {
      throw new Error(`无效 fixed_size 格式：${fixed}`);
    }
    const { w, h } = parsed;
    const reason = validateExplicitSize(w, h);
    if (reason) throw new Error(reason);
    return {
      size: `${w}x${h}` as `${number}x${number}`,
      width: w,
      height: h,
      prompt_suffix: "",
    };
  }

  // fixed 为空：回退到 aspect preset
  const { w, h } = PRESET[aspect] ?? fallbackByBudget(aspect);
  return {
    size: `${w}x${h}` as `${number}x${number}`,
    width: w,
    height: h,
    prompt_suffix: "",
  };
}

function fallbackByBudget(aspect: AspectRatio): { w: number; h: number } {
  const r = RATIO_MAP[aspect];
  let W = alignDown(Math.sqrt((PIXEL_BUDGET * r.w) / r.h));
  let H = heightForWidth(W, r);
  while (W > 0 && (H <= 0 || !fitsBudget(W, H))) {
    W -= EXPLICIT_ALIGN;
    H = heightForWidth(W, r);
  }
  if (W <= 0 || H <= 0 || !fitsBudget(W, H)) {
    throw new Error(`无法在像素预算内解析尺寸：${aspect}`);
  }
  return { w: W, h: H };
}

function alignDown(value: number): number {
  return Math.floor(value / EXPLICIT_ALIGN) * EXPLICIT_ALIGN;
}

function heightForWidth(
  width: number,
  ratio: { w: number; h: number },
): number {
  return alignDown((width * ratio.h) / ratio.w);
}

function fitsBudget(w: number, h: number): boolean {
  return w * h <= PIXEL_BUDGET;
}
