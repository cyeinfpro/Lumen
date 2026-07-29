import {
  type Dispatch,
  type RefObject,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  applyMaskShortcut,
  resolveMaskShortcut,
  shouldIgnoreMaskShortcut,
} from "../maskBoardKeyboard";
import type { Stroke, Tool } from "../types";
import {
  estimateLiveCoverage,
  estimateLuminance,
  exportMaskCanvas,
} from "./canvasRendering";
import {
  clampBrush,
  DEFAULT_BRUSH_DESKTOP,
  DEFAULT_BRUSH_TOUCH,
  defaultViewTransform,
  displayDimensions,
  isTouchDevice,
  isViewTransformFit,
  maskColorsForLuminance,
} from "./geometry";
import type {
  ContainerDimensions,
  DisplayDimensions,
  MaskBoardStats,
  MaskCursor,
  MaskExport,
  ViewTransform,
} from "./types";

const IMAGE_RETRY_DELAYS = [120, 320, 1000] as const;
const STROKES_DEBOUNCE_MS = 380;

interface UseMaskBoardStateOptions {
  imageSrc: string;
  initialStrokes?: Stroke[] | null;
  onStrokesChange?: (strokes: Stroke[]) => void;
  onStatsChange?: (stats: MaskBoardStats) => void;
}

export interface MaskBoardState {
  boardAreaRef: RefObject<HTMLDivElement | null>;
  imgEl: HTMLImageElement | null;
  imgError: string | null;
  imgFadeIn: boolean;
  displayDims: DisplayDimensions;
  displayKey: string;
  tool: Tool;
  setTool: Dispatch<SetStateAction<Tool>>;
  brushSize: number;
  setBrushSize: Dispatch<SetStateAction<number>>;
  strokes: Stroke[];
  setStrokes: Dispatch<SetStateAction<Stroke[]>>;
  cursor: MaskCursor | null;
  setCursor: Dispatch<SetStateAction<MaskCursor | null>>;
  view: ViewTransform;
  setView: Dispatch<SetStateAction<ViewTransform>>;
  hasStroke: boolean;
  viewIsFit: boolean;
  liveCoverage: number;
  isDarkBg: boolean;
  overlayColor: string;
  cursorStroke: string;
  cursorFill: string;
  exportMask: () => Promise<MaskExport | null>;
  retryImage: () => void;
  undo: () => void;
  reset: () => void;
}

export function useMaskBoardState({
  imageSrc,
  initialStrokes,
  onStrokesChange,
  onStatsChange,
}: UseMaskBoardStateOptions): MaskBoardState {
  const boardAreaRef = useRef<HTMLDivElement | null>(null);
  const [containerDims, setContainerDims] =
    useState<ContainerDimensions | null>(null);
  const [imgEl, setImgEl] = useState<HTMLImageElement | null>(null);
  const [imgError, setImgError] = useState<string | null>(null);
  const [retryAttempt, setRetryAttempt] = useState(0);
  const [tool, setTool] = useState<Tool>("brush");
  const [brushSize, setBrushSize] = useState(() =>
    isTouchDevice() ? DEFAULT_BRUSH_TOUCH : DEFAULT_BRUSH_DESKTOP,
  );
  const [strokes, setStrokes] = useState<Stroke[]>(() => initialStrokes ?? []);
  const [cursor, setCursor] = useState<MaskCursor | null>(null);
  const [imgFadeIn, setImgFadeIn] = useState(false);
  const [luminance, setLuminance] = useState(0.6);
  const [view, setView] = useState<ViewTransform>(defaultViewTransform);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [prevImageSrc, setPrevImageSrc] = useState(imageSrc);
  if (prevImageSrc !== imageSrc) {
    setPrevImageSrc(imageSrc);
    setImgEl(null);
    setImgError(null);
    setImgFadeIn(false);
    setStrokes(initialStrokes ?? []);
    setRetryAttempt(0);
    setView(defaultViewTransform());
  }

  useEffect(() => {
    if (!imageSrc) return;
    let alive = true;
    const image = new window.Image();
    image.crossOrigin = "anonymous";
    image.onload = () => {
      if (!alive) return;
      setImgEl(image);
      setLuminance(estimateLuminance(image));
      requestAnimationFrame(() => {
        if (alive) setImgFadeIn(true);
      });
    };
    image.onerror = () => {
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
    image.src = imageSrc;
    return () => {
      alive = false;
      image.onload = null;
      image.onerror = null;
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
    };
  }, [imageSrc, retryAttempt]);

  useEffect(() => {
    const element = boardAreaRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      if (width <= 0 || height <= 0) return;
      setContainerDims({ w: Math.floor(width), h: Math.floor(height) });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const displayDims = useMemo(
    () => displayDimensions(imgEl, containerDims),
    [containerDims, imgEl],
  );
  const displayKey = `${displayDims.width}x${displayDims.height}`;
  const [prevDisplayKey, setPrevDisplayKey] = useState(displayKey);
  if (prevDisplayKey !== displayKey) {
    setPrevDisplayKey(displayKey);
    setView(defaultViewTransform());
  }

  const hasStroke = strokes.length > 0;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (shouldIgnoreMaskShortcut(event)) return;
      const shortcut = resolveMaskShortcut(event.key);
      if (!shortcut) return;
      const handled = applyMaskShortcut(shortcut, hasStroke, {
        setTool,
        setStrokes,
        setBrushSize,
        clampBrush,
      });
      if (handled) event.preventDefault();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [hasStroke]);

  useEffect(() => {
    if (!onStrokesChange) return;
    const timeout = setTimeout(
      () => onStrokesChange(strokes),
      STROKES_DEBOUNCE_MS,
    );
    return () => clearTimeout(timeout);
  }, [onStrokesChange, strokes]);

  const liveCoverage = useMemo(
    () => (imgEl ? estimateLiveCoverage(displayDims, strokes) : 0),
    [displayDims, imgEl, strokes],
  );
  useEffect(() => {
    onStatsChange?.({ coverage: liveCoverage, strokeCount: strokes.length });
  }, [liveCoverage, onStatsChange, strokes.length]);

  const exportMask = useCallback(
    () => exportMaskCanvas(imgEl, displayDims.scale, strokes),
    [displayDims.scale, imgEl, strokes],
  );
  const retryImage = useCallback(() => {
    setImgError(null);
    setRetryAttempt(0);
  }, []);
  const undo = useCallback(() => {
    setStrokes((current) => current.slice(0, -1));
  }, []);
  const reset = useCallback(() => {
    setStrokes([]);
  }, []);
  const colors = maskColorsForLuminance(luminance);

  return {
    boardAreaRef,
    imgEl,
    imgError,
    imgFadeIn,
    displayDims,
    displayKey,
    tool,
    setTool,
    brushSize,
    setBrushSize,
    strokes,
    setStrokes,
    cursor,
    setCursor,
    view,
    setView,
    hasStroke,
    viewIsFit: isViewTransformFit(view),
    liveCoverage,
    ...colors,
    exportMask,
    retryImage,
    undo,
    reset,
  };
}
