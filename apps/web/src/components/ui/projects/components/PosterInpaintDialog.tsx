"use client";

// 局部 inpaint dialog：
// - Canvas 涂抹 mask（圆形笔刷）
// - 文本输入"编辑意图"
// - 提交：upload mask 图 → 调 inpaint API
//
// mask 协议：黑底白笔；上传是 PNG，后端会做二值化和 alpha 处理。
// Canvas 使用原生 <canvas>，不引入 Konva 等大库。

import { Eraser, Loader2, Paintbrush, Trash2, Upload, X } from "lucide-react";
import Image from "next/image";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import { uploadImage } from "@/lib/apiClient";
import type { BackendImageMeta } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { imageSrc } from "../utils";

const MAX_CANVAS_PX = 1024; // 内部画布上限；上传 mask 会按显示尺寸生成
const FALLBACK_CONTAINER_PX = 720; // RO 测到容器前的兜底尺寸（外层 overflow-hidden 兜底）
const BRUSH_DEFAULT = 36;
const BRUSH_MIN = 8;
const BRUSH_MAX = 128;

interface PosterInpaintDialogProps {
  open: boolean;
  onClose: () => void;
  image: BackendImageMeta;
  busy?: boolean;
  onSubmit: (input: { instruction: string; mask_image_id: string }) => void;
}

export function PosterInpaintDialog({
  open,
  onClose,
  image,
  busy = false,
  onSubmit,
}: PosterInpaintDialogProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [brush, setBrush] = useState(BRUSH_DEFAULT);
  const [mode, setMode] = useState<"draw" | "erase">("draw");
  const [instruction, setInstruction] = useState("");
  const [hasStrokes, setHasStrokes] = useState(false);
  const [uploadingMask, setUploadingMask] = useState(false);
  const drawingRef = useRef(false);
  const lastPosRef = useRef<{ x: number; y: number } | null>(null);

  // 派生 canvas 尺寸：限制内部分辨率 ≤ MAX_CANVAS_PX，保持图片宽高比。
  // React 19：不在 effect 里 setState，直接 useMemo 算。
  const canvasSize = useMemo(() => {
    const w = image.width || 1024;
    const h = image.height || 1024;
    const ratio = Math.min(MAX_CANVAS_PX / Math.max(w, h), 1);
    return {
      width: Math.round(w * ratio),
      height: Math.round(h * ratio),
    };
  }, [image.width, image.height]);

  // 容器实测尺寸：原写死 `width: min(100%, 720px)` + aspect-ratio + max-h-full 在矮容器 /
  // 高瘦图（9:16 等）下会被夹成"指甲盖"。改 ResizeObserver fit 容器算 displayDims。
  const [containerDims, setContainerDims] = useState<{ w: number; h: number } | null>(null);
  useEffect(() => {
    if (!open) return;
    const el = containerRef.current;
    if (!el) return;
    const measure = () => {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        setContainerDims({ w: Math.floor(rect.width), h: Math.floor(rect.height) });
      }
    };
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0]?.contentRect;
      if (!cr || cr.width <= 0 || cr.height <= 0) return;
      setContainerDims({ w: Math.floor(cr.width), h: Math.floor(cr.height) });
    });
    ro.observe(el);
    const raf = window.requestAnimationFrame(measure);
    return () => {
      window.cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [open]);

  // contain fit：按 canvasSize 比例缩到容器内（不超出，不强行铺满）
  const displayDims = useMemo(() => {
    const w = canvasSize.width;
    const h = canvasSize.height;
    if (!w || !h) return { width: 0, height: 0 };
    const availW = containerDims ? containerDims.w : FALLBACK_CONTAINER_PX;
    const availH = containerDims ? containerDims.h : FALLBACK_CONTAINER_PX;
    const scale = Math.min(availW / w, availH / h, 1);
    return {
      width: Math.max(1, Math.round(w * scale)),
      height: Math.max(1, Math.round(h * scale)),
    };
  }, [canvasSize.width, canvasSize.height, containerDims]);

  // 打开 / canvas 尺寸变化时重置 canvas + 表单
  useEffect(() => {
    if (!open) return;
    if (!canvasRef.current) return;
    const ctx = canvasRef.current.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvasRef.current.width, canvasRef.current.height);
    setHasStrokes(false);
    setInstruction("");
  }, [open, canvasSize.width, canvasSize.height]);

  // pointer 绘制：圆形笔刷
  const toCanvasCoord = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return {
      x: (event.clientX - rect.left) * scaleX,
      y: (event.clientY - rect.top) * scaleY,
    };
  };

  const drawStroke = (x: number, y: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    if (mode === "erase") {
      ctx.globalCompositeOperation = "destination-out";
      ctx.fillStyle = "rgba(0,0,0,1)";
    } else {
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = "rgba(255,255,255,0.95)";
    }
    ctx.beginPath();
    ctx.arc(x, y, brush / 2, 0, Math.PI * 2);
    ctx.fill();

    // 与上一点连线（更顺滑）
    const last = lastPosRef.current;
    if (last && (Math.abs(last.x - x) > 1 || Math.abs(last.y - y) > 1)) {
      ctx.lineWidth = brush;
      ctx.lineCap = "round";
      ctx.strokeStyle =
        mode === "erase" ? "rgba(0,0,0,1)" : "rgba(255,255,255,0.95)";
      ctx.beginPath();
      ctx.moveTo(last.x, last.y);
      ctx.lineTo(x, y);
      ctx.stroke();
    }
    lastPosRef.current = { x, y };
    if (!hasStrokes && mode === "draw") setHasStrokes(true);
  };

  const onPointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    event.preventDefault();
    drawingRef.current = true;
    event.currentTarget.setPointerCapture(event.pointerId);
    const pos = toCanvasCoord(event);
    lastPosRef.current = null;
    drawStroke(pos.x, pos.y);
  };

  const onPointerMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (!drawingRef.current) return;
    const pos = toCanvasCoord(event);
    drawStroke(pos.x, pos.y);
  };

  const onPointerUp = (event: React.PointerEvent<HTMLCanvasElement>) => {
    drawingRef.current = false;
    lastPosRef.current = null;
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      // 某些浏览器 throw；忽略
    }
  };

  const clearCanvas = () => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    setHasStrokes(false);
  };

  // 生成 PNG mask：黑底 + 白色涂抹区
  const buildMaskBlob = (): Promise<Blob | null> => {
    return new Promise((resolve) => {
      const src = canvasRef.current;
      if (!src) {
        resolve(null);
        return;
      }
      const out = document.createElement("canvas");
      out.width = src.width;
      out.height = src.height;
      const ctx = out.getContext("2d");
      if (!ctx) {
        resolve(null);
        return;
      }
      // 黑底
      ctx.fillStyle = "#000000";
      ctx.fillRect(0, 0, out.width, out.height);
      // 用涂抹图（含 alpha）覆盖在黑底上：alpha>0 → 白色
      const tmp = document.createElement("canvas");
      tmp.width = src.width;
      tmp.height = src.height;
      const tmpCtx = tmp.getContext("2d");
      if (!tmpCtx) {
        resolve(null);
        return;
      }
      tmpCtx.drawImage(src, 0, 0);
      const data = tmpCtx.getImageData(0, 0, src.width, src.height);
      for (let i = 0; i < data.data.length; i += 4) {
        const alpha = data.data[i + 3];
        if (alpha > 8) {
          data.data[i] = 255;
          data.data[i + 1] = 255;
          data.data[i + 2] = 255;
          data.data[i + 3] = 255;
        } else {
          data.data[i] = 0;
          data.data[i + 1] = 0;
          data.data[i + 2] = 0;
          data.data[i + 3] = 255;
        }
      }
      ctx.putImageData(data, 0, 0);
      out.toBlob((blob) => resolve(blob), "image/png");
    });
  };

  const handleSubmit = async () => {
    const trimmed = instruction.trim();
    if (!hasStrokes) {
      toast.error("请先涂抹要修复的区域");
      return;
    }
    if (!trimmed) {
      toast.error("请输入编辑意图");
      return;
    }
    setUploadingMask(true);
    try {
      const blob = await buildMaskBlob();
      if (!blob) {
        toast.error("生成 mask 图失败");
        return;
      }
      const file = new File([blob], `mask_${Date.now()}.png`, {
        type: "image/png",
      });
      const uploaded = await uploadImage(file);
      onSubmit({ instruction: trimmed, mask_image_id: uploaded.id });
    } catch (err) {
      toast.error("上传 mask 失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      });
    } finally {
      setUploadingMask(false);
    }
  };

  // ESC 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const submitBusy = busy || uploadingMask;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="局部修复"
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-stretch justify-center bg-black/65 backdrop-blur-sm md:items-center"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitBusy) onClose();
      }}
    >
      <div className="mobile-dialog-panel relative flex h-[var(--mobile-dialog-max-height)] w-full max-w-[1100px] flex-col overflow-hidden bg-[var(--bg-0)] shadow-[var(--shadow-2)] max-md:rounded-t-[var(--radius-sheet)] md:h-[min(86vh,720px)] md:rounded-lg md:border md:border-[var(--border)]">
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-4">
          <div className="min-w-0">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Inpaint
            </p>
            <h2 className="type-section-title mt-1">局部修复</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={submitBusy}
            aria-label="关闭"
            className="inline-flex h-9 w-9 cursor-pointer items-center justify-center rounded-full text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] disabled:opacity-50"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="mobile-dialog-scroll grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[minmax(0,1fr)_320px] lg:overflow-hidden">
          <div className="relative flex h-[min(58dvh,460px)] min-h-[300px] min-w-0 flex-col bg-[var(--bg-1)] lg:h-auto lg:min-h-0">
            <div
              ref={containerRef}
              className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden p-3 sm:p-4"
            >
              <div
                className="relative"
                style={{
                  width: displayDims.width,
                  height: displayDims.height,
                }}
              >
                <Image
                  src={imageSrc(image)}
                  alt="目标图片"
                  fill
                  sizes="720px"
                  unoptimized
                  className="pointer-events-none select-none object-contain"
                />
                <canvas
                  ref={canvasRef}
                  width={canvasSize.width}
                  height={canvasSize.height}
                  className={cn(
                    "absolute inset-0 h-full w-full touch-none",
                    mode === "draw" ? "cursor-crosshair" : "cursor-cell",
                  )}
                  style={{ opacity: 0.55 }}
                  onPointerDown={onPointerDown}
                  onPointerMove={onPointerMove}
                  onPointerUp={onPointerUp}
                  onPointerCancel={onPointerUp}
                />
              </div>
            </div>

            <div className="shrink-0 border-t border-[var(--border)] px-4 py-3">
              <div className="flex min-w-0 flex-wrap items-center gap-3">
                <div className="inline-flex min-w-0 rounded-full border border-[var(--border)] p-0.5">
                  <button
                    type="button"
                    onClick={() => setMode("draw")}
                    className={cn(
                      "inline-flex h-8 items-center gap-1.5 rounded-full px-3 font-mono text-[10px] uppercase tracking-[0.18em] transition-colors",
                      mode === "draw"
                        ? "bg-[var(--amber-400)] text-[var(--accent-on)]"
                        : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
                    )}
                  >
                    <Paintbrush className="h-3.5 w-3.5" />
                    涂抹
                  </button>
                  <button
                    type="button"
                    onClick={() => setMode("erase")}
                    className={cn(
                      "inline-flex h-8 items-center gap-1.5 rounded-full px-3 font-mono text-[10px] uppercase tracking-[0.18em] transition-colors",
                      mode === "erase"
                        ? "bg-[var(--amber-400)] text-[var(--accent-on)]"
                        : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
                    )}
                  >
                    <Eraser className="h-3.5 w-3.5" />
                    擦除
                  </button>
                </div>

                <label className="flex min-w-0 flex-1 items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)] sm:flex-none">
                  画笔
                  <input
                    type="range"
                    min={BRUSH_MIN}
                    max={BRUSH_MAX}
                    step={2}
                    value={brush}
                    onChange={(event) => setBrush(Number(event.target.value))}
                    className="min-w-0 flex-1 accent-[var(--amber-400)] sm:w-28 sm:flex-none"
                  />
                  <span className="tabular-nums text-[var(--fg-1)]">{brush}</span>
                </label>

                <Button
                  variant="ghost"
                  size="sm"
                  onClick={clearCanvas}
                  leftIcon={<Trash2 className="h-3.5 w-3.5" />}
                >
                  清空
                </Button>
              </div>
            </div>
          </div>

          <aside className="flex min-h-0 shrink-0 flex-col border-t border-[var(--border)] lg:border-l lg:border-t-0">
            <div className="mobile-dialog-scroll flex-1 overflow-y-auto px-5 py-4">
              <label className="block min-w-0">
                <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
                  编辑意图
                </span>
                <textarea
                  value={instruction}
                  onChange={(event) =>
                    setInstruction(event.target.value.slice(0, 600))
                  }
                  rows={6}
                  maxLength={600}
                  placeholder="例如：把右上角的杂物去掉，保持背景简洁"
                  className="mt-2 w-full resize-y border-b border-[var(--border)] bg-transparent px-1 py-2 text-[14px] leading-6 text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
                />
                <span className="mt-2 block text-right font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                  {instruction.length} / 600
                </span>
              </label>

              <p className="mt-4 text-[12px] leading-[1.7] text-[var(--fg-2)]">
                涂抹要修改的区域 → 在右侧输入修改意图 → 提交。mask 会自动生成 PNG（黑底白笔）。
              </p>
            </div>

            <div className="mobile-dialog-footer shrink-0 border-t border-[var(--border)] px-5 py-4">
              <Button
                variant="primary"
                fullWidth
                loading={submitBusy}
                onClick={handleSubmit}
                disabled={!hasStrokes || !instruction.trim()}
                leftIcon={
                  submitBusy ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Upload className="h-4 w-4" />
                  )
                }
              >
                {uploadingMask ? "上传 Mask" : "提交修复"}
              </Button>
            </div>
          </aside>
        </div>
      </div>
    </div>
  );
}
