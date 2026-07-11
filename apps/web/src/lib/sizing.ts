// 前端尺寸预设与比例工具。服务端负责最终 fixed_size 校验与解析。

import type {
  AspectRatio,
  ImageOutputFormat,
  Quality,
  RenderQuality,
} from "./types";

const RATIO_MAP: Record<AspectRatio, { w: number; h: number }> = {
  "1:1": { w: 1, h: 1 },
  "16:9": { w: 16, h: 9 },
  "9:16": { w: 9, h: 16 },
  "21:9": { w: 21, h: 9 },
  "9:21": { w: 9, h: 21 },
  "10:7": { w: 10, h: 7 },
  "7:10": { w: 7, h: 10 },
  "4:5": { w: 4, h: 5 },
  "3:4": { w: 3, h: 4 },
  "4:3": { w: 4, h: 3 },
  "3:2": { w: 3, h: 2 },
  "2:3": { w: 2, h: 3 },
};

// 默认 preset：按"默认最大 4K 画质 + 按比例分配"的策略升级。
// 横/竖构图配对：3:2↔2:3 / 4:3↔3:4 / 16:9↔9:16 / 21:9↔9:21
export const PRESET: Record<AspectRatio, { w: number; h: number }> = {
  "1:1":  { w: 2880, h: 2880 }, // 8,294,400
  "16:9": { w: 3840, h: 2160 }, // 8,294,400
  "9:16": { w: 2160, h: 3840 },
  "21:9": { w: 3808, h: 1632 }, // 6,214,656
  "9:21": { w: 1632, h: 3808 },
  "10:7": { w: 3424, h: 2400 }, // 8,217,600
  "7:10": { w: 2400, h: 3424 },
  "4:5":  { w: 2560, h: 3200 }, // 8,192,000
  "3:4":  { w: 2448, h: 3264 }, // 7,989,072
  "4:3":  { w: 3264, h: 2448 },
  "3:2":  { w: 3504, h: 2336 }, // 8,185,344
  "2:3":  { w: 2336, h: 3504 },
};

const PRESET_1K: Record<AspectRatio, { w: number; h: number }> = {
  "1:1":  { w: 1024, h: 1024 },
  "16:9": { w: 1536, h: 864 },
  "9:16": { w: 864, h: 1536 },
  "21:9": { w: 1536, h: 656 },
  "9:21": { w: 656, h: 1536 },
  "10:7": { w: 1344, h: 944 },
  "7:10": { w: 944, h: 1344 },
  "4:5":  { w: 1024, h: 1280 },
  "3:4":  { w: 1024, h: 1360 },
  "4:3":  { w: 1360, h: 1024 },
  "3:2":  { w: 1440, h: 960 },
  "2:3":  { w: 960, h: 1440 },
};

const PRESET_2K: Record<AspectRatio, { w: number; h: number }> = {
  "1:1":  { w: 1440, h: 1440 },
  "16:9": { w: 2048, h: 1152 },
  "9:16": { w: 1152, h: 2048 },
  "21:9": { w: 2240, h: 960 },
  "9:21": { w: 960, h: 2240 },
  "10:7": { w: 1920, h: 1344 },
  "7:10": { w: 1344, h: 1920 },
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

/**
 * 给定原图宽高，返回 RATIO_MAP 中**最接近**的 AspectRatio。
 * 用 log-ratio 距离（对横竖构图对称），避免 16:9 / 21:9 在大宽比下被误并到 1:1。
 *
 * 用例：局部修改（inpaint）从源图反推比例；用户上传图反推默认比例。
 * 退化：w/h 非有效正数时返回 "1:1"（最安全的 fallback，不会引入构图偏差）。
 */
export function nearestAspectRatio(
  w: number | null | undefined,
  h: number | null | undefined,
): AspectRatio {
  if (
    typeof w !== "number" ||
    typeof h !== "number" ||
    !Number.isFinite(w) ||
    !Number.isFinite(h) ||
    w <= 0 ||
    h <= 0
  ) {
    return "1:1";
  }
  const target = Math.log(w / h);
  let best: AspectRatio = "1:1";
  let bestDist = Number.POSITIVE_INFINITY;
  for (const [key, { w: rw, h: rh }] of Object.entries(RATIO_MAP) as Array<
    [AspectRatio, { w: number; h: number }]
  >) {
    const dist = Math.abs(Math.log(rw / rh) - target);
    if (dist < bestDist) {
      bestDist = dist;
      best = key;
    }
  }
  return best;
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
