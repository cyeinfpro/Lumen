"use client";

// 局部修改 (inpaint) 画布。给一张参考图，用户用画笔涂出"要被重画"的区域。
// 设计要点：
//   - 显示尺寸 ≤ 768px（保持比例），导出时按 (原图宽/显示宽) 的 pixelRatio 还原到原图分辨率。
//   - 用户笔画在 Konva 上以红色半透明 overlay 实时显示（hex ≈ #ff3b3080）；
//     橡皮擦用 destination-out 在 overlay 上抠透明，视觉上"擦掉"红色。
//   - 导出 mask 时不直接走 stage.toCanvas，因为屏上是红色 overlay；改成离屏白底
//     画 strokes，brush 用 destination-out 把白色抠成透明 → 得到 RGBA PNG（涂抹处 alpha=0）。
//
// 设计 V1 边界：
//   - 撤销栈仅维护本次会话；关闭弹窗后状态丢失（即"已 mask"显示，但二次进入是空白画布）
//   - 不做客户端羽化、不做 SAM、不做模板选区
//   - React 19 注意：不要在 render 里访问 stageRef，stage 操作走 onClick 之类事件 handler。

import { AnimatePresence, motion } from "framer-motion";
import { Eraser, Loader2, Paintbrush, RotateCcw, Undo2, X } from "lucide-react";
import {
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Image as KonvaImage, Layer, Line, Stage } from "react-konva";
import type Konva from "konva";

import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

const MAX_DISPLAY = 768; // 长边像素上限：足够看清细节，又不至于让 Konva 一次画太多点
const MIN_BRUSH = 10;
const MAX_BRUSH = 80;
const DEFAULT_BRUSH = 40;

// 涂满判定阈值：> 95% 提示用户"基本全覆盖了"，但仍允许提交（业务上有人就是要重画几乎全图）。
const FULL_COVERAGE_WARN = 0.95;

// hex #ff3b3080 ≈ rgba(255, 59, 48, 0.5) — Apple destructive red 调色 + 半透明
const OVERLAY_STROKE = "rgba(255, 59, 48, 0.5)";

type Tool = "brush" | "eraser";

interface Stroke {
  tool: Tool;
  radius: number;
  // 扁平 [x1, y1, x2, y2, ...] 给 Konva.Line 用
  points: number[];
}

export interface MaskExport {
  blob: Blob;
  preview_data_url: string;
  width: number; // 原图宽
  height: number; // 原图高
  /** 涂抹覆盖比例（用于阈值警告，0..1） */
  coverage: number;
}

interface MaskCanvasProps {
  open: boolean;
  /** 原图 data URL（与 attachment.data_url 一致） */
  imageSrc: string;
  /** 关闭弹窗（无论确认还是取消都会被调用前置） */
  onClose: () => void;
  /** 用户点击"确认"后回调，外部负责 toBlob → uploadImage → setMask */
  onConfirm: (mask: MaskExport) => void | Promise<void>;
  /** 提交中（外部上传 mask 的过程），用于按钮 loading 与禁用关闭 */
  submitting?: boolean;
}

// 外层只做 mount 门控：open=true 才挂内部 panel。
// 内部 panel 每次 mount 都是全新状态，避免 React 19 react-hooks/set-state-in-effect 的 reset 副作用。
export function MaskCanvas(props: MaskCanvasProps) {
  if (!props.open) return null;
  return <MaskCanvasInner {...props} />;
}

function MaskCanvasInner({
  imageSrc,
  onClose,
  onConfirm,
  submitting,
}: MaskCanvasProps) {
  const stageRef = useRef<Konva.Stage | null>(null);
  const [imgEl, setImgEl] = useState<HTMLImageElement | null>(null);
  const [imgError, setImgError] = useState<string | null>(null);
  const [tool, setTool] = useState<Tool>("brush");
  const [brushSize, setBrushSize] = useState(DEFAULT_BRUSH);
  const [strokes, setStrokes] = useState<Stroke[]>([]);
  const drawingRef = useRef(false);

  // ———— 加载原图（HTMLImageElement，给 Konva 使用） ————
  // setState 全部发生在 onload/onerror 回调里 —— 不属于"effect 同步 setState"，符合 react-hooks/set-state-in-effect。
  useEffect(() => {
    if (!imageSrc) return;
    let alive = true;
    const el = new window.Image();
    el.crossOrigin = "anonymous";
    el.onload = () => {
      if (alive) setImgEl(el);
    };
    el.onerror = () => {
      if (alive) setImgError("无法加载图片");
    };
    el.src = imageSrc;
    return () => {
      alive = false;
      el.onload = null;
      el.onerror = null;
    };
  }, [imageSrc]);

  // ———— Esc 关闭 + body 滚动锁 ————
  // 用 ref 读最新 submitting，避免 onKey/effect 因 prop 变更频繁重新绑定。
  // React 19 不允许 render 阶段访问 ref —— 用单独 effect 同步当前值到 ref。
  const submittingRef = useRef(submitting);
  useEffect(() => {
    submittingRef.current = submitting;
  }, [submitting]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submittingRef.current) {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  // ———— 显示尺寸：把原图缩放到长边 MAX_DISPLAY ————
  const displayDims = useMemo(() => {
    if (!imgEl) return { width: 0, height: 0, scale: 1 };
    const { naturalWidth: w, naturalHeight: h } = imgEl;
    if (!w || !h) return { width: 0, height: 0, scale: 1 };
    const scale = Math.min(1, MAX_DISPLAY / Math.max(w, h));
    return {
      width: Math.round(w * scale),
      height: Math.round(h * scale),
      scale,
    };
  }, [imgEl]);

  const hasStroke = strokes.length > 0;

  // ———— 笔画事件：pointer down/move/up ————
  const handlePointerDown = useCallback(
    (e: Konva.KonvaEventObject<PointerEvent>) => {
      if (submitting) return;
      const stage = e.target.getStage();
      const pos = stage?.getPointerPosition();
      if (!pos) return;
      drawingRef.current = true;
      // 立即追加一个新 stroke，含起点
      setStrokes((prev) => [
        ...prev,
        { tool, radius: brushSize, points: [pos.x, pos.y] },
      ]);
    },
    [tool, brushSize, submitting],
  );

  const handlePointerMove = useCallback(
    (e: Konva.KonvaEventObject<PointerEvent>) => {
      if (!drawingRef.current) return;
      if (submitting) return;
      const stage = e.target.getStage();
      const pos = stage?.getPointerPosition();
      if (!pos) return;
      // 把新点 push 到最近一笔；用 functional update 避免闭包陈旧
      setStrokes((prev) => {
        if (prev.length === 0) return prev;
        const last = prev[prev.length - 1];
        const next: Stroke = {
          ...last,
          points: [...last.points, pos.x, pos.y],
        };
        return [...prev.slice(0, -1), next];
      });
    },
    [submitting],
  );

  const handlePointerUp = useCallback(() => {
    drawingRef.current = false;
  }, []);

  const handleUndo = useCallback(() => {
    setStrokes((prev) => prev.slice(0, -1));
  }, []);

  const handleReset = useCallback(() => {
    setStrokes([]);
  }, []);

  // ———— 导出：把 strokes 反投到原图分辨率画到离屏 canvas，destination-out 抠透明 ————
  const exportMask = useCallback(async (): Promise<MaskExport | null> => {
    if (!imgEl) return null;
    const { naturalWidth: W, naturalHeight: H } = imgEl;
    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    // 1) 不透明白底（alpha=255）
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, W, H);
    // 2) 反向显示坐标 → 原图坐标的缩放因子
    const inv = displayDims.scale === 0 ? 1 : 1 / displayDims.scale;
    // 3) 按 strokes 顺序：brush = destination-out（抠透明），eraser = destination-over 重补白
    //    （eraser 在显示层是 destination-out 红 overlay；导出语义是"恢复 alpha=255"）
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    for (const s of strokes) {
      if (s.points.length < 2) continue;
      ctx.beginPath();
      ctx.moveTo(s.points[0] * inv, s.points[1] * inv);
      for (let i = 2; i < s.points.length; i += 2) {
        ctx.lineTo(s.points[i] * inv, s.points[i + 1] * inv);
      }
      // points 只有一个点时画成 dot：lineTo 同点 + lineWidth=2*r 的圆头线
      if (s.points.length === 2) {
        ctx.lineTo(s.points[0] * inv + 0.01, s.points[1] * inv);
      }
      ctx.lineWidth = s.radius * 2 * inv;
      if (s.tool === "brush") {
        ctx.globalCompositeOperation = "destination-out";
        ctx.strokeStyle = "rgba(0,0,0,1)";
      } else {
        // 橡皮擦：恢复白色 alpha=255
        ctx.globalCompositeOperation = "source-over";
        ctx.strokeStyle = "#ffffff";
      }
      ctx.stroke();
    }
    ctx.globalCompositeOperation = "source-over";

    // 4) 算覆盖率（透明像素占比）—— 防 95% 全覆盖警告
    let transparent = 0;
    const data = ctx.getImageData(0, 0, W, H).data;
    for (let i = 3; i < data.length; i += 4) {
      if (data[i] === 0) transparent += 1;
    }
    const coverage = transparent / (W * H);

    // 5) toBlob（PNG，保留 alpha 通道）
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob((b) => resolve(b), "image/png"),
    );
    if (!blob) return null;

    // 预览 dataURL（用于"已设置 mask"缩略图）
    const preview_data_url = canvas.toDataURL("image/png");

    return {
      blob,
      preview_data_url,
      width: W,
      height: H,
      coverage,
    };
  }, [imgEl, strokes, displayDims.scale]);

  const [warning, setWarning] = useState<string | null>(null);

  const handleConfirm = useCallback(async () => {
    if (!hasStroke || submitting) return;
    setWarning(null);
    const m = await exportMask();
    if (!m) {
      setWarning("导出 mask 失败，请重试");
      return;
    }
    if (m.coverage > FULL_COVERAGE_WARN) {
      // 仅做提示：仍允许提交（V1 行为）
      setWarning(
        `已涂抹约 ${(m.coverage * 100).toFixed(0)}%，几乎全图重画 — 可继续，或撤销几笔后再试`,
      );
    }
    await onConfirm(m);
  }, [hasStroke, submitting, exportMask, onConfirm]);

  // ———— 触屏: 阻止默认手势避免拖滚动 ————
  const onContainerPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      // 仅在画布区域阻止；按钮/滑块不会触发，因为这是容器层
      if ((e.target as HTMLElement).closest("[data-mask-canvas-stage]")) {
        e.preventDefault();
      }
    },
    [],
  );

  return (
    <AnimatePresence>
      <motion.div
        key="mask-overlay"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.16 }}
        className={cn(
          // 复用 dialog 的 z-index 通道
          "fixed inset-0 z-[var(--z-dialog)]",
          "bg-black/72 backdrop-blur-md",
          "flex items-center justify-center",
          "p-3 sm:p-6",
        )}
        onPointerDown={onContainerPointerDown}
      >
        <motion.div
          role="dialog"
          aria-modal="true"
          aria-label="局部修改 mask 画布"
          initial={{ opacity: 0, scale: 0.96, y: 8 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.96, y: 8 }}
          transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
          className={cn(
            "w-full max-w-[860px]",
            "max-h-[calc(100dvh-1.5rem)] sm:max-h-[calc(100dvh-3rem)]",
            "flex flex-col overflow-hidden",
            "rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]",
            "shadow-[var(--shadow-2)]",
          )}
        >
          {/* Header */}
          <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-[var(--border-subtle)]">
            <div className="flex flex-col">
              <h2 className="type-card-title">局部修改</h2>
              <p className="type-body-sm text-[var(--fg-1)]">
                涂抹要被重画的区域；红色高亮即 mask
              </p>
            </div>
            <button
              type="button"
              onClick={() => {
                if (submitting) return;
                onClose();
              }}
              disabled={submitting}
              aria-label="关闭"
              className={cn(
                "shrink-0 inline-flex items-center justify-center w-9 h-9 rounded-full",
                "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-2)]",
                "disabled:opacity-40 disabled:cursor-not-allowed",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
              )}
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* 画布区域 */}
          <div className="relative flex-1 min-h-0 overflow-auto bg-[var(--bg-0)]">
            <div className="flex items-center justify-center min-h-full p-4">
              {imgError ? (
                <div className="text-sm text-[var(--danger)]">{imgError}</div>
              ) : !imgEl ? (
                <div className="flex items-center gap-2 text-sm text-[var(--fg-1)]">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  正在载入图片…
                </div>
              ) : (
                <div
                  data-mask-canvas-stage
                  className={cn(
                    "relative rounded-lg overflow-hidden border border-[var(--border-subtle)]",
                    "shadow-[var(--shadow-1)]",
                    "touch-none select-none",
                  )}
                  style={{
                    width: displayDims.width,
                    height: displayDims.height,
                  }}
                >
                  <Stage
                    ref={stageRef}
                    width={displayDims.width}
                    height={displayDims.height}
                    onPointerDown={handlePointerDown}
                    onPointerMove={handlePointerMove}
                    onPointerUp={handlePointerUp}
                    onPointerLeave={handlePointerUp}
                  >
                    {/* 底层：原图 */}
                    <Layer listening={false}>
                      <KonvaImage
                        image={imgEl}
                        width={displayDims.width}
                        height={displayDims.height}
                      />
                    </Layer>
                    {/* 上层：红色 overlay 笔画。eraser 用 destination-out 抠掉红色。 */}
                    <Layer listening={false}>
                      {strokes.map((s, i) => (
                        <Line
                          key={i}
                          points={s.points}
                          stroke={OVERLAY_STROKE}
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
                </div>
              )}
            </div>
          </div>

          {/* 警告 */}
          <AnimatePresence>
            {warning && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.18 }}
                className="overflow-hidden border-t border-[var(--border-subtle)]"
              >
                <div
                  className={cn(
                    "px-4 py-2 text-xs",
                    "bg-[var(--amber-400)]/10 text-[var(--amber-400)]",
                  )}
                >
                  {warning}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* 工具条 */}
          <div className="flex flex-wrap items-center gap-2 px-4 py-3 border-t border-[var(--border-subtle)]">
            <ToolSegment value={tool} onChange={setTool} />

            <label className="flex items-center gap-2 ml-2">
              <span className="text-[11px] text-[var(--fg-1)] tabular-nums w-10">
                {brushSize}px
              </span>
              <input
                type="range"
                min={MIN_BRUSH}
                max={MAX_BRUSH}
                step={2}
                value={brushSize}
                onChange={(e) => setBrushSize(Number(e.target.value))}
                aria-label="画笔大小"
                className="h-1.5 w-32 cursor-pointer accent-[var(--amber-400)]"
              />
            </label>

            <button
              type="button"
              onClick={handleUndo}
              disabled={!hasStroke || submitting}
              aria-label="撤销"
              title="撤销"
              className={cn(
                "inline-flex items-center justify-center w-9 h-9 rounded-full",
                "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-2)]",
                "disabled:opacity-40 disabled:cursor-not-allowed",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
              )}
            >
              <Undo2 className="w-4 h-4" />
            </button>

            <button
              type="button"
              onClick={handleReset}
              disabled={!hasStroke || submitting}
              aria-label="重置"
              title="重置"
              className={cn(
                "inline-flex items-center justify-center w-9 h-9 rounded-full",
                "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-2)]",
                "disabled:opacity-40 disabled:cursor-not-allowed",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
              )}
            >
              <RotateCcw className="w-4 h-4" />
            </button>

            <div className="flex-1" />

            <Button
              variant="ghost"
              size="sm"
              onClick={onClose}
              disabled={submitting}
            >
              取消
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => void handleConfirm()}
              disabled={!hasStroke || submitting}
              loading={submitting}
            >
              确认
            </Button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

function ToolSegment({
  value,
  onChange,
}: {
  value: Tool;
  onChange: (v: Tool) => void;
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
          { v: "brush" as const, label: "画笔", Icon: Paintbrush },
          { v: "eraser" as const, label: "橡皮", Icon: Eraser },
        ]
      ).map(({ v, label, Icon }) => {
        const active = value === v;
        return (
          <button
            key={v}
            type="button"
            onClick={() => onChange(v)}
            aria-pressed={active}
            aria-label={label}
            title={label}
            className={cn(
              "inline-flex items-center gap-1.5 h-8 px-3 rounded-full",
              "text-[11px] transition-colors",
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
