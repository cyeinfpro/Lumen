"use client";

// LightboxImage —— 负责图片本体的 FLIP 入场 + motion transform。
//
// 入场：从 `fromRect`（点击源卡片 DOMRect）线性变换到目标居中位置。
// 若图解码失败或没有 fromRect，退化为 fade-in。
//
// motion values 在调用方管理，这里只读；translateX/Y/scale/opacity 都由父传入。

import { motion, type MotionValue } from "framer-motion";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { cn } from "@/lib/utils";

export interface LightboxImageProps {
  id: string;
  url: string;
  alt?: string;
  aspect?: number | null; // width / height（可选，用于计算目标尺寸）
  fromRect: DOMRect | null;
  /** 是否在 reduced-motion 下 */
  reducedMotion?: boolean;
  /** 由手势层驱动 */
  dragX: MotionValue<number>;
  dragY: MotionValue<number>;
  scale: MotionValue<number>;
}

interface DecodeState {
  url: string;
  decoded: boolean;
  failed: boolean;
}

function subscribeViewport(onStoreChange: () => void) {
  if (typeof window === "undefined") return () => {};
  window.addEventListener("resize", onStoreChange);
  window.addEventListener("orientationchange", onStoreChange);
  return () => {
    window.removeEventListener("resize", onStoreChange);
    window.removeEventListener("orientationchange", onStoreChange);
  };
}

function getViewportSnapshot() {
  if (typeof window === "undefined") return "0x0";
  return `${window.innerWidth}x${window.innerHeight}`;
}

/** 计算目标矩形（在视口中居中，满足 100vw × 80vh 约束）。 */
function computeTarget(
  aspect: number | null | undefined,
  viewportW: number,
  viewportH: number,
) {
  if (viewportW <= 0 || viewportH <= 0) {
    return { x: 0, y: 0, w: 0, h: 0 };
  }
  const maxW = viewportW;
  const maxH = viewportH * 0.8;
  const ar = aspect && aspect > 0 ? aspect : maxW / maxH;
  let w = maxW;
  let h = w / ar;
  if (h > maxH) {
    h = maxH;
    w = h * ar;
  }
  const x = (viewportW - w) / 2;
  const y = (viewportH - h) / 2;
  return { x, y, w, h };
}

export function LightboxImage({
  id,
  url,
  alt,
  aspect,
  fromRect,
  reducedMotion = false,
  dragX,
  dragY,
  scale,
}: LightboxImageProps) {
  const imgRef = useRef<HTMLImageElement | null>(null);
  const decodeSeqRef = useRef(0);
  const [decodeState, setDecodeState] = useState<DecodeState>(() => ({
    url,
    decoded: false,
    failed: false,
  }));
  const decoded = decodeState.url === url && decodeState.decoded;
  const decodeFailed = decodeState.url === url && decodeState.failed;
  const viewport = useSyncExternalStore(
    subscribeViewport,
    getViewportSnapshot,
    () => "0x0",
  );
  const [viewportW, viewportH] = viewport
    .split("x")
    .map((n) => Number.parseInt(n, 10));

  // 预解码：success → 使用 FLIP；failure → fade-in
  useEffect(() => {
    const seq = decodeSeqRef.current + 1;
    decodeSeqRef.current = seq;
    let canceled = false;
    const im = new Image();
    im.src = url;
    const p = typeof im.decode === "function" ? im.decode() : Promise.resolve();
    p.then(() => {
      if (!canceled && decodeSeqRef.current === seq) {
        setDecodeState({ url, decoded: true, failed: false });
      }
    }).catch(() => {
      if (!canceled && decodeSeqRef.current === seq) {
        setDecodeState({ url, decoded: false, failed: true });
      }
    });
    return () => {
      canceled = true;
      decodeSeqRef.current += 1;
      im.onload = null;
      im.onerror = null;
      im.src = "";
    };
  }, [url]);

  // FLIP 参数（仅首渲染读一次 fromRect → 转为 motion initial）
  const target = useMemo(
    () => computeTarget(aspect ?? null, viewportW, viewportH),
    [aspect, viewportW, viewportH],
  );
  const canFlip = !reducedMotion && !decodeFailed && fromRect && target.w > 0;
  const initial = canFlip
    ? {
        opacity: 1,
        x: fromRect!.left - target.x,
        y: fromRect!.top - target.y,
        scaleX: fromRect!.width / target.w,
        scaleY: fromRect!.height / target.h,
      }
    : {
        opacity: 0,
        x: 0,
        y: 0,
        scaleX: 1,
        scaleY: 1,
      };

  return (
    <motion.div
      className="absolute left-1/2 top-1/2 pointer-events-none"
      style={{
        width: target.w || undefined,
        height: target.h || undefined,
        translateX: "-50%",
        translateY: "-50%",
      }}
    >
      <motion.div
        className="w-full h-full will-change-transform"
        style={{ x: dragX, y: dragY, scale }}
      >
        <motion.img
          ref={imgRef}
          key={`${id}:${url}`}
          src={url}
          alt={alt ?? ""}
          draggable={false}
          initial={initial}
          animate={{ opacity: 1, x: 0, y: 0, scaleX: 1, scaleY: 1 }}
          transition={
            reducedMotion
              ? { duration: 0.18, ease: "linear" }
              : { type: "spring", damping: 30, stiffness: 260, mass: 0.9 }
          }
          className={cn(
            "w-full h-full object-contain select-none pointer-events-auto",
            "rounded-md shadow-2xl",
          )}
          style={{ touchAction: "none", transformOrigin: "top left" }}
          onLoad={() => {
            decodeSeqRef.current += 1;
            setDecodeState({ url, decoded: true, failed: false });
          }}
          onError={() => {
            decodeSeqRef.current += 1;
            setDecodeState({ url, decoded: false, failed: true });
          }}
        />
      </motion.div>
      {/* decoded 状态供父组件可选读取（目前只用于内部容错） */}
      <span className="sr-only" aria-hidden>
        {decoded ? "" : "loading"}
      </span>
    </motion.div>
  );
}
