"use client";

// MaskBoard —— 局部修改画板（纯组件，不含 dialog 外壳）。
// 由 MaskCanvas（Composer 旧入口）和 InpaintModal（独立全局入口）共同复用。
//
// 画板对外通过 imperative ref 暴露 exportMask / hasStrokes / clear / coverageEstimate。
//
// 加固/优化：
//   - 图加载失败 3 次退避重试（100/300/1000ms）
//   - 大图 export coverage 采样（>=1.5K 像素按 1/9 采样估算，避免 4K 图卡顿）
//   - stroke 抽稀（同点 < 1.2px 不 push，防长涂时点过多）
//   - 触屏（pointerType=touch）不渲染光标预览
//   - 快捷键：B/E 切工具，Z 撤销，[/] 减/增画笔大小
//   - 鼠标在画板上滚轮：调画笔大小（不滚动页面）
//   - 实时显示涂抹覆盖比例（基于显示尺寸下采样估算）
//   - 暗图自动切换 mask 颜色（avg luminance < 0.45 用 cyan，否则 red）
//   - imageSrc 变更时 prev-check reset（React 19 不在 effect 同步 setState）
//
// 已知 V1 边界：撤销栈仅维护本次会话；二次 mount 是空白画布。

import { Eraser, Loader2, Paintbrush, RotateCcw, Undo2 } from "lucide-react";
import {
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { Image as KonvaImage, Layer, Line, Stage } from "react-konva";
import type Konva from "konva";

import { Button, IconButton } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import type { Stroke, Tool } from "./types";
export type { Stroke, Tool } from "./types";

const MAX_DISPLAY = 768;
const MIN_BRUSH = 8;
const MAX_BRUSH = 96;
const BRUSH_STEP = 4;
const DEFAULT_BRUSH_DESKTOP = 36;
const DEFAULT_BRUSH_TOUCH = 56;
const STROKE_MIN_DELTA_SQ = 1.44; // 1.2px 平方；同笔内距离过近的点抽稀掉
const COVERAGE_SAMPLE_MAX_PIXELS = 1024 * 1024; // 100 万像素以上走采样
const COVERAGE_SAMPLE_STRIDE = 3; // 每 3x3 取 1 个像素
const IMAGE_RETRY_DELAYS = [120, 320, 1000] as const;
const STROKES_DEBOUNCE_MS = 380; // onStrokesChange 去抖：避免逐点触发存储

// 数字键 1-9 → 画笔预设大小
const BRUSH_PRESETS: Record<string, number> = {
  "1": 12,
  "2": 18,
  "3": 26,
  "4": 36,
  "5": 48,
  "6": 60,
  "7": 72,
  "8": 84,
  "9": 96,
};

// hex #ff3b3080 ≈ Apple destructive red 调色 + 半透明
const OVERLAY_RED = "rgba(255, 59, 48, 0.5)";
const CURSOR_RED_STROKE = "rgba(255, 59, 48, 0.92)";
const CURSOR_RED_FILL = "rgba(255, 59, 48, 0.16)";
// 暗图回退色：青色对比强
const OVERLAY_CYAN = "rgba(64, 224, 208, 0.55)";
const CURSOR_CYAN_STROKE = "rgba(64, 224, 208, 0.95)";
const CURSOR_CYAN_FILL = "rgba(64, 224, 208, 0.18)";

export interface MaskExport {
  blob: Blob;
  preview_data_url: string;
  width: number;
  height: number;
  /** 涂抹覆盖比例（0..1，alpha=0 像素占比；大图采样估算） */
  coverage: number;
}

export interface MaskBoardHandle {
  exportMask: () => Promise<MaskExport | null>;
  hasStrokes: () => boolean;
  clear: () => void;
}

interface MaskBoardProps {
  imageSrc: string;
  /** 提交中：禁止笔画与工具切换 */
  disabled?: boolean;
  /** 初始 strokes（用于回填上次未提交的草稿，仅 mount 时生效） */
  initialStrokes?: Stroke[] | null;
  /** strokes 变化（去抖 380ms）— 父组件可写入 store 持久化 */
  onStrokesChange?: (strokes: Stroke[]) => void;
  /** 实时统计回调（覆盖率 0..1，stroke 数量） */
  onStatsChange?: (stats: { coverage: number; strokeCount: number }) => void;
  className?: string;
  style?: CSSProperties;
}

function isTouchDevice(): boolean {
  if (typeof window === "undefined") return false;
  return "ontouchstart" in window || (navigator.maxTouchPoints ?? 0) > 0;
}

function clampBrush(v: number): number {
  return Math.max(MIN_BRUSH, Math.min(MAX_BRUSH, Math.round(v)));
}

// 估算 imgEl 的平均亮度，用于决定 mask 颜色（暗图用 cyan，亮图用 red）。
// 走一个 32x32 的 offscreen sample，足够精度且零成本。
function estimateLuminance(img: HTMLImageElement): number {
  try {
    const c = document.createElement("canvas");
    c.width = 32;
    c.height = 32;
    const ctx = c.getContext("2d");
    if (!ctx) return 0.6;
    ctx.drawImage(img, 0, 0, 32, 32);
    const data = ctx.getImageData(0, 0, 32, 32).data;
    let sum = 0;
    for (let i = 0; i < data.length; i += 4) {
      // ITU-R BT.601 亮度近似
      sum += data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114;
    }
    return sum / (32 * 32 * 255);
  } catch {
    return 0.6;
  }
}

export const MaskBoard = forwardRef<MaskBoardHandle, MaskBoardProps>(
  function MaskBoard(
    {
      imageSrc,
      disabled,
      initialStrokes,
      onStrokesChange,
      onStatsChange,
      className,
      style,
    },
    ref,
  ) {
    const stageRef = useRef<Konva.Stage | null>(null);
    const containerRef = useRef<HTMLDivElement | null>(null);
    const boardAreaRef = useRef<HTMLDivElement | null>(null);
    // 画板区域实测尺寸；ResizeObserver 维护，让画布严格 fit 容器避免溢出滚动
    // —— 旧实现固定 MAX_DISPLAY=768 + overflow-auto，导致：图大于容器时要滚动看全貌，
    // 但 wheel 又被 handleWheel 吃掉调画笔，用户根本滚不动 → 死锁。fit 之后 wheel 完全归画笔，无冲突。
    const [containerDims, setContainerDims] = useState<{
      w: number;
      h: number;
    } | null>(null);
    const [imgEl, setImgEl] = useState<HTMLImageElement | null>(null);
    const [imgError, setImgError] = useState<string | null>(null);
    // retryAttempt 直接作为 effect 的 reload signal + retry 计数：每次 onerror +1，达 max 后停。
    // imageSrc 变化时 prev-check 同步 reset 为 0。
    const [retryAttempt, setRetryAttempt] = useState(0);
    const [tool, setTool] = useState<Tool>("brush");
    const [brushSize, setBrushSize] = useState<number>(() =>
      isTouchDevice() ? DEFAULT_BRUSH_TOUCH : DEFAULT_BRUSH_DESKTOP,
    );
    // initialStrokes 仅 mount 时生效；source 切换由 imageSrc prev-check 接管 reset
    const [strokes, setStrokes] = useState<Stroke[]>(
      () => initialStrokes ?? [],
    );
    const [cursor, setCursor] = useState<{
      x: number;
      y: number;
      pointerType: "mouse" | "pen" | "touch" | null;
    } | null>(null);
    const [imgFadeIn, setImgFadeIn] = useState(false);
    const [luminance, setLuminance] = useState<number>(0.6);
    const drawingRef = useRef(false);
    const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // React 19：imageSrc 变更时 prev-check 同步 reset state，不在 effect 里 setState。
    // 切换图时用新图的 initialStrokes 回填（如果有）。
    const [prevImageSrc, setPrevImageSrc] = useState(imageSrc);
    if (prevImageSrc !== imageSrc) {
      setPrevImageSrc(imageSrc);
      setImgEl(null);
      setImgError(null);
      setImgFadeIn(false);
      setStrokes(initialStrokes ?? []);
      setRetryAttempt(0);
    }

    // ———— 加载原图（含 3 次退避重试） ————
    // effect 跑在 [imageSrc, retryAttempt] 上：onerror 失败时 setRetryAttempt+1 触发重新加载。
    useEffect(() => {
      if (!imageSrc) return;
      let alive = true;
      const el = new window.Image();
      el.crossOrigin = "anonymous";
      el.onload = () => {
        if (!alive) return;
        setImgEl(el);
        setLuminance(estimateLuminance(el));
        requestAnimationFrame(() => {
          if (alive) setImgFadeIn(true);
        });
      };
      el.onerror = () => {
        if (!alive) return;
        if (retryAttempt < IMAGE_RETRY_DELAYS.length) {
          const delay = IMAGE_RETRY_DELAYS[retryAttempt];
          retryTimerRef.current = setTimeout(() => {
            if (alive) setRetryAttempt(retryAttempt + 1);
          }, delay);
        } else {
          setImgError("图片加载失败");
        }
      };
      el.src = imageSrc;
      return () => {
        alive = false;
        el.onload = null;
        el.onerror = null;
        if (retryTimerRef.current) {
          clearTimeout(retryTimerRef.current);
          retryTimerRef.current = null;
        }
      };
    }, [imageSrc, retryAttempt]);

    const isDarkBg = luminance < 0.45;
    const overlayColor = isDarkBg ? OVERLAY_CYAN : OVERLAY_RED;
    const cursorStroke = isDarkBg ? CURSOR_CYAN_STROKE : CURSOR_RED_STROKE;
    const cursorFill = isDarkBg ? CURSOR_CYAN_FILL : CURSOR_RED_FILL;

    // ———— 容器尺寸监听：让画板严格 fit 容器（不溢出） ————
    // ResizeObserver 在 observe() 后会自动派发一次首帧 callback（spec 行为），
    // 不需要在 effect 同步 setState（避免 React 19 编译器的 effect-setstate lint 风险）。
    useEffect(() => {
      const el = boardAreaRef.current;
      if (!el) return;
      const ro = new ResizeObserver((entries) => {
        const e = entries[0];
        if (!e) return;
        const cr = e.contentRect;
        if (cr.width <= 0 || cr.height <= 0) return;
        setContainerDims({
          w: Math.floor(cr.width),
          h: Math.floor(cr.height),
        });
      });
      ro.observe(el);
      return () => ro.disconnect();
    }, []);

    // ———— 显示尺寸：原图按容器实测尺寸 fit + 长边 ≤ MAX_DISPLAY ————
    const displayDims = useMemo(() => {
      if (!imgEl) return { width: 0, height: 0, scale: 1 };
      const { naturalWidth: w, naturalHeight: h } = imgEl;
      if (!w || !h) return { width: 0, height: 0, scale: 1 };
      // 容器未测到（首帧）走 MAX_DISPLAY；测到后用容器尺寸（已扣 padding）
      const availW = containerDims ? containerDims.w : MAX_DISPLAY;
      const availH = containerDims ? containerDims.h : MAX_DISPLAY;
      const scale = Math.min(
        1,
        availW / w,
        availH / h,
        MAX_DISPLAY / Math.max(w, h),
      );
      return {
        width: Math.round(w * scale),
        height: Math.round(h * scale),
        scale,
      };
    }, [imgEl, containerDims]);

    const hasStroke = strokes.length > 0;

    // ———— 全局快捷键 ————
    // B/E 切工具，Z 撤销，[/] 调大小，1-9 预设大小
    useEffect(() => {
      const onKey = (e: KeyboardEvent) => {
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        const target = e.target as HTMLElement | null;
        if (
          target &&
          (target.tagName === "INPUT" ||
            target.tagName === "TEXTAREA" ||
            target.isContentEditable)
        ) {
          return;
        }
        switch (e.key) {
          case "b":
          case "B":
            e.preventDefault();
            setTool("brush");
            return;
          case "e":
          case "E":
            e.preventDefault();
            setTool("eraser");
            return;
          case "z":
          case "Z":
            if (hasStroke) {
              e.preventDefault();
              setStrokes((prev) => prev.slice(0, -1));
            }
            return;
          case "[":
            e.preventDefault();
            setBrushSize((v) => clampBrush(v - BRUSH_STEP));
            return;
          case "]":
            e.preventDefault();
            setBrushSize((v) => clampBrush(v + BRUSH_STEP));
            return;
          default:
            if (e.key in BRUSH_PRESETS) {
              e.preventDefault();
              setBrushSize(BRUSH_PRESETS[e.key]);
            }
            return;
        }
      };
      document.addEventListener("keydown", onKey);
      return () => document.removeEventListener("keydown", onKey);
    }, [hasStroke]);

    // ———— strokes 持久化（去抖触发 onStrokesChange） ————
    useEffect(() => {
      if (!onStrokesChange) return;
      const id = setTimeout(
        () => onStrokesChange(strokes),
        STROKES_DEBOUNCE_MS,
      );
      return () => clearTimeout(id);
    }, [strokes, onStrokesChange]);

    // ———— 笔画事件 ————
    // pen 启用笔压感：radius = brushSize * (0.4 + pressure * 0.6)，0.4..1x 之间
    // mouse / touch 设备 pressure 不可信，固定 1x
    const handlePointerDown = useCallback(
      (e: Konva.KonvaEventObject<PointerEvent>) => {
        if (disabled) return;
        const stage = e.target.getStage();
        const pos = stage?.getPointerPosition();
        if (!pos) return;
        const native = e.evt as PointerEvent;
        // 仅主键 / 触摸 / 笔触发笔画；忽略右键
        if (native.button !== undefined && native.button > 0) return;
        drawingRef.current = true;
        const isPen = native.pointerType === "pen";
        const pressure =
          typeof native.pressure === "number" ? native.pressure : 0;
        const sizeMultiplier =
          isPen && pressure > 0 ? 0.4 + pressure * 0.6 : 1;
        const effectiveRadius = Math.max(
          Math.round(MIN_BRUSH / 2),
          Math.round(brushSize * sizeMultiplier),
        );
        setStrokes((prev) => [
          ...prev,
          { tool, radius: effectiveRadius, points: [pos.x, pos.y] },
        ]);
      },
      [tool, brushSize, disabled],
    );

    const handlePointerMove = useCallback(
      (e: Konva.KonvaEventObject<PointerEvent>) => {
        if (disabled) return;
        const stage = e.target.getStage();
        const pos = stage?.getPointerPosition();
        if (!pos) return;
        const native = e.evt as PointerEvent;
        const pt =
          (native.pointerType as "mouse" | "pen" | "touch" | undefined) ??
          "mouse";
        setCursor({ x: pos.x, y: pos.y, pointerType: pt });
        if (!drawingRef.current) return;
        // stroke 抽稀：与上一点距离 < 1.2px 时不 push
        setStrokes((prev) => {
          if (prev.length === 0) return prev;
          const last = prev[prev.length - 1];
          const lx = last.points[last.points.length - 2];
          const ly = last.points[last.points.length - 1];
          const dx = pos.x - lx;
          const dy = pos.y - ly;
          if (dx * dx + dy * dy < STROKE_MIN_DELTA_SQ) return prev;
          const next: Stroke = {
            ...last,
            points: [...last.points, pos.x, pos.y],
          };
          return [...prev.slice(0, -1), next];
        });
      },
      [disabled],
    );

    const handlePointerUp = useCallback(() => {
      drawingRef.current = false;
    }, []);

    const handlePointerLeave = useCallback(() => {
      drawingRef.current = false;
      setCursor(null);
    }, []);

    const handleUndo = useCallback(() => {
      setStrokes((prev) => prev.slice(0, -1));
    }, []);

    const handleReset = useCallback(() => {
      setStrokes([]);
    }, []);

    // 画板上的滚轮：调画笔大小，吃掉默认页面滚动
    const handleWheel = useCallback(
      (e: ReactWheelEvent<HTMLDivElement>) => {
        if (disabled) return;
        if (e.deltaY === 0) return;
        e.preventDefault();
        e.stopPropagation();
        const dir = e.deltaY < 0 ? 1 : -1;
        setBrushSize((v) => clampBrush(v + dir * BRUSH_STEP));
      },
      [disabled],
    );

    // ———— 显示尺寸下的覆盖率估算（实时显示用，便宜） ————
    // 不能等到 export 才知道：用户希望涂的过程中看到自己进度。
    // 走显示分辨率的 canvas（< 768px），按 stride 采样，性能可接受。
    const liveCoverage = useMemo(() => {
      if (!imgEl || !displayDims.width || !displayDims.height) return 0;
      if (strokes.length === 0) return 0;
      const W = displayDims.width;
      const H = displayDims.height;
      const c = document.createElement("canvas");
      c.width = W;
      c.height = H;
      const ctx = c.getContext("2d");
      if (!ctx) return 0;
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, W, H);
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      for (const s of strokes) {
        if (s.points.length < 2) continue;
        ctx.beginPath();
        ctx.moveTo(s.points[0], s.points[1]);
        for (let i = 2; i < s.points.length; i += 2) {
          ctx.lineTo(s.points[i], s.points[i + 1]);
        }
        if (s.points.length === 2) {
          ctx.lineTo(s.points[0] + 0.01, s.points[1]);
        }
        ctx.lineWidth = s.radius * 2;
        if (s.tool === "brush") {
          ctx.globalCompositeOperation = "destination-out";
          ctx.strokeStyle = "rgba(0,0,0,1)";
        } else {
          ctx.globalCompositeOperation = "source-over";
          ctx.strokeStyle = "#fff";
        }
        ctx.stroke();
      }
      ctx.globalCompositeOperation = "source-over";
      const data = ctx.getImageData(0, 0, W, H).data;
      let transparent = 0;
      let count = 0;
      const stride = COVERAGE_SAMPLE_STRIDE;
      for (let y = 0; y < H; y += stride) {
        for (let x = 0; x < W; x += stride) {
          const idx = (y * W + x) * 4 + 3;
          if (data[idx] === 0) transparent += 1;
          count += 1;
        }
      }
      return count === 0 ? 0 : transparent / count;
    }, [imgEl, displayDims.width, displayDims.height, strokes]);

    // 上报统计（每次 strokes 或 coverage 变化）
    useEffect(() => {
      onStatsChange?.({ coverage: liveCoverage, strokeCount: strokes.length });
    }, [liveCoverage, strokes.length, onStatsChange]);

    // ———— 导出 ————
    // 大图（>= COVERAGE_SAMPLE_MAX_PIXELS）的 coverage 走采样，保留近似精度。
    const exportMask = useCallback(async (): Promise<MaskExport | null> => {
      if (!imgEl) return null;
      const { naturalWidth: W, naturalHeight: H } = imgEl;
      if (!W || !H) return null;
      const canvas = document.createElement("canvas");
      canvas.width = W;
      canvas.height = H;
      const ctx = canvas.getContext("2d");
      if (!ctx) return null;
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, W, H);
      const inv = displayDims.scale === 0 ? 1 : 1 / displayDims.scale;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      for (const s of strokes) {
        if (s.points.length < 2) continue;
        ctx.beginPath();
        ctx.moveTo(s.points[0] * inv, s.points[1] * inv);
        for (let i = 2; i < s.points.length; i += 2) {
          ctx.lineTo(s.points[i] * inv, s.points[i + 1] * inv);
        }
        if (s.points.length === 2) {
          ctx.lineTo(s.points[0] * inv + 0.01, s.points[1] * inv);
        }
        ctx.lineWidth = s.radius * 2 * inv;
        if (s.tool === "brush") {
          ctx.globalCompositeOperation = "destination-out";
          ctx.strokeStyle = "rgba(0,0,0,1)";
        } else {
          ctx.globalCompositeOperation = "source-over";
          ctx.strokeStyle = "#ffffff";
        }
        ctx.stroke();
      }
      ctx.globalCompositeOperation = "source-over";

      // 覆盖率：大图采样，小图全像素
      let coverage = 0;
      const total = W * H;
      if (total > COVERAGE_SAMPLE_MAX_PIXELS) {
        const stride = COVERAGE_SAMPLE_STRIDE;
        const data = ctx.getImageData(0, 0, W, H).data;
        let transparent = 0;
        let count = 0;
        for (let y = 0; y < H; y += stride) {
          for (let x = 0; x < W; x += stride) {
            const idx = (y * W + x) * 4 + 3;
            if (data[idx] === 0) transparent += 1;
            count += 1;
          }
        }
        coverage = count === 0 ? 0 : transparent / count;
      } else {
        const data = ctx.getImageData(0, 0, W, H).data;
        let transparent = 0;
        for (let i = 3; i < data.length; i += 4) {
          if (data[i] === 0) transparent += 1;
        }
        coverage = transparent / total;
      }

      const blob = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob((b) => resolve(b), "image/png"),
      );
      if (!blob) return null;
      const preview_data_url = canvas.toDataURL("image/png");

      return { blob, preview_data_url, width: W, height: H, coverage };
    }, [imgEl, strokes, displayDims.scale]);

    useImperativeHandle(
      ref,
      () => ({
        exportMask,
        hasStrokes: () => strokes.length > 0,
        clear: () => setStrokes([]),
      }),
      [exportMask, strokes.length],
    );

    // 触屏：阻止默认手势避免拖滚动 / 长按选择
    const onContainerPointerDown = useCallback(
      (e: ReactPointerEvent<HTMLDivElement>) => {
        if ((e.target as HTMLElement).closest("[data-mask-canvas-stage]")) {
          e.preventDefault();
        }
      },
      [],
    );

    const handleManualRetry = useCallback(() => {
      setImgError(null);
      // 已经达 max 失败时 retryAttempt = IMAGE_RETRY_DELAYS.length；
      // 设回 0 触发 effect 重跑。
      setRetryAttempt(0);
    }, []);

    return (
      <div
        ref={containerRef}
        className={cn("flex flex-col gap-3", className)}
        style={style}
        onPointerDown={onContainerPointerDown}
      >
        {/* 画板区域：ResizeObserver 测内容尺寸 → displayDims fit；不再 overflow-auto */}
        <div
          ref={boardAreaRef}
          className={cn(
            "relative flex-1 min-h-0 rounded-lg bg-[var(--bg-0)]",
            "p-2 sm:p-4 overflow-hidden",
            "flex items-center justify-center",
          )}
          onWheel={handleWheel}
        >
          <div className="flex items-center justify-center w-full h-full">
            {imgError ? (
              <div className="flex flex-col items-center gap-2 type-body-sm text-[var(--danger-fg)]">
                <span>{imgError}</span>
                <Button
                  variant="link"
                  onClick={handleManualRetry}
                >
                  重试
                </Button>
              </div>
            ) : !imgEl ? (
              <div className="flex items-center gap-2 type-body-sm text-[var(--fg-1)]">
                <Loader2 className="w-4 h-4 animate-spin" />
                加载中
              </div>
            ) : (
              <div
                data-mask-canvas-stage
                className={cn(
                  "relative rounded-lg overflow-hidden border border-[var(--border-subtle)]",
                  "shadow-[var(--shadow-1)]",
                  "touch-none select-none",
                  !isTouchDevice() && "cursor-crosshair",
                )}
                style={{
                  width: displayDims.width,
                  height: displayDims.height,
                  opacity: imgFadeIn ? 1 : 0,
                  transition: "opacity 220ms ease-out",
                }}
              >
                <Stage
                  ref={stageRef}
                  width={displayDims.width}
                  height={displayDims.height}
                  onPointerDown={handlePointerDown}
                  onPointerMove={handlePointerMove}
                  onPointerUp={handlePointerUp}
                  onPointerLeave={handlePointerLeave}
                >
                  <Layer listening={false}>
                    <KonvaImage
                      image={imgEl}
                      width={displayDims.width}
                      height={displayDims.height}
                    />
                  </Layer>
                  <Layer listening={false}>
                    {strokes.map((s, i) => (
                      <Line
                        key={i}
                        points={s.points}
                        stroke={overlayColor}
                        strokeWidth={s.radius * 2}
                        tension={0}
                        lineCap="round"
                        lineJoin="round"
                        globalCompositeOperation={
                          s.tool === "brush"
                            ? "source-over"
                            : "destination-out"
                        }
                      />
                    ))}
                  </Layer>
                </Stage>

                {/* 光标预览（仅 mouse/pen 显示；触屏隐藏避免重影） */}
                {cursor &&
                  cursor.pointerType !== "touch" &&
                  !disabled && (
                    <div
                      aria-hidden
                      className="pointer-events-none absolute"
                      style={{
                        left: cursor.x - brushSize,
                        top: cursor.y - brushSize,
                        width: brushSize * 2,
                        height: brushSize * 2,
                        borderRadius: "50%",
                        border: `1.5px solid ${cursorStroke}`,
                        background:
                          tool === "brush" ? cursorFill : "transparent",
                        boxShadow: "0 0 0 1px rgba(0,0,0,0.32) inset",
                      }}
                    />
                  )}
              </div>
            )}
          </div>
        </div>

        {/* 工具条 */}
        <div className="flex flex-wrap items-center gap-2 px-1">
          <ToolSegment value={tool} onChange={setTool} disabled={disabled} />

          <BrushSizeControl
            value={brushSize}
            onChange={(v) => setBrushSize(clampBrush(v))}
            disabled={disabled}
            isDarkBg={isDarkBg}
          />

          <IconButton
            variant="ghost"
            onClick={handleUndo}
            disabled={!hasStroke || disabled}
            aria-label="撤销 (Z)"
            tooltip="撤销 (Z)"
            className="rounded-full"
          >
            <Undo2 className="w-4 h-4" />
          </IconButton>

          <IconButton
            variant="ghost"
            onClick={handleReset}
            disabled={!hasStroke || disabled}
            aria-label="清除全部"
            tooltip="清除全部"
            className="rounded-full"
          >
            <RotateCcw className="w-4 h-4" />
          </IconButton>

          {/* 实时覆盖率 */}
          <CoverageBadge
            coverage={liveCoverage}
            strokeCount={strokes.length}
          />
        </div>
      </div>
    );
  },
);

function ToolSegment({
  value,
  onChange,
  disabled,
}: {
  value: Tool;
  onChange: (v: Tool) => void;
  disabled?: boolean;
}) {
  return (
    <div
      role="group"
      aria-label="工具"
      className={cn(
        "shrink-0 inline-flex items-center h-9 p-px rounded-full",
        "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
      )}
    >
      {(
        [
          { v: "brush" as const, label: "画笔", hint: "B", Icon: Paintbrush },
          { v: "eraser" as const, label: "橡皮", hint: "E", Icon: Eraser },
        ]
      ).map(({ v, label, hint, Icon }) => {
        const active = value === v;
        return (
          <button
            key={v}
            type="button"
            onClick={() => onChange(v)}
            disabled={disabled}
            aria-pressed={active}
            aria-label={`${label} (${hint})`}
            title={`${label} (${hint})`}
            className={cn(
              "inline-flex items-center gap-1.5 h-8 px-3 rounded-full",
              "text-[11px] transition-colors disabled:opacity-50",
              active
                ? "bg-[var(--bg-0)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
            )}
          >
            <Icon className="w-3.5 h-3.5" aria-hidden />
            <span>{label}</span>
          </button>
        );
      })}
    </div>
  );
}

function BrushSizeControl({
  value,
  onChange,
  disabled,
  isDarkBg,
}: {
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
  isDarkBg: boolean;
}) {
  // 静态预览圆：直观反馈当前 brush size，最大不超过 30px 视觉
  const previewDiameter = Math.min(30, Math.max(6, value / 3));
  return (
    <label
      className="flex items-center gap-2 ml-1"
      title="画笔大小（[ / ] 调节，鼠标滚轮调节）"
    >
      <span
        aria-hidden
        className="inline-flex items-center justify-center w-5 h-5"
      >
        <span
          style={{
            width: previewDiameter,
            height: previewDiameter,
            borderRadius: "50%",
            background: isDarkBg
              ? "rgba(64, 224, 208, 0.55)"
              : "rgba(255, 59, 48, 0.5)",
            transition: "width 80ms ease-out, height 80ms ease-out",
          }}
        />
      </span>
      <input
        type="range"
        min={MIN_BRUSH}
        max={MAX_BRUSH}
        step={2}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        disabled={disabled}
        aria-label="画笔大小"
        className="h-1.5 w-28 sm:w-32 cursor-pointer accent-[var(--amber-400)] disabled:cursor-not-allowed"
      />
      <span className="text-[11px] text-[var(--fg-1)] tabular-nums w-9">
        {value}px
      </span>
    </label>
  );
}

function CoverageBadge({
  coverage,
  strokeCount,
}: {
  coverage: number;
  strokeCount: number;
}) {
  if (strokeCount === 0) return null;
  const pct = Math.round(coverage * 100);
  // 颜色分档：< 5% 中性，5-30% success（合适），>= 50% warning（警示）
  const tone =
    pct >= 50
      ? "bg-warning-soft text-warning border-warning-border"
      : pct >= 5
        ? "bg-success-soft text-success border-success-border"
        : "bg-[var(--bg-2)] text-[var(--fg-1)] border-[var(--border-subtle)]";
  return (
    <span
      className={cn(
        "ml-auto inline-flex items-center gap-1 h-7 px-2.5 rounded-full",
        "text-[11px] tabular-nums border",
        tone,
      )}
      aria-live="polite"
    >
      涂抹 {pct}%
    </span>
  );
}
