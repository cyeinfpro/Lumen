"use client";

import {
  Check,
  Copy,
  Crosshair,
  Download,
  Image as ImageIcon,
  ImageDown,
  RotateCcw,
} from "lucide-react";
import { useRouter } from "next/navigation";
import {
  memo,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type SyntheticEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  ActionSheet,
  pushMobileToast,
} from "@/components/ui/primitives/mobile";
import type {
  PrewarmHandle,
  PrewarmScheduler,
} from "../model/prewarmScheduler";
import type { GenerationSummary } from "../model/contracts";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/useChatStore";
import {
  buildGeneratedImage,
  createGenerationTileModel,
  imageDownloadName,
  type GenerationTileModel,
} from "../model/tileModel";
import {
  assetCandidateSrcSet,
  failedAssetCandidateSource,
  healthyAssetCandidates,
} from "../model/sourceCandidates";

export interface GenerationTileProps {
  item: GenerationSummary;
  onOpen: (itemId: string, rect: DOMRect) => void;
  selectionMode?: boolean;
  selected?: boolean;
  onToggleSelect?: (imageId: string) => void;
  prewarmScheduler: PrewarmScheduler;
  fetchPriority?: "high" | "low" | "auto";
  imageSizes?: string;
}

const LONG_PRESS_MS = 420;
const TAP_FEEDBACK_MS = 180;
const PRESS_MOVE_SLOP_PX = 10;

function isTileActivationKey(key: string): boolean {
  return key === "Enter" || key === " ";
}

function ensureImageInChatStore(item: GenerationSummary) {
  const imageId = item.image.id;
  if (useChatStore.getState().imagesById[imageId]) return;
  const image = buildGeneratedImage(item);
  useChatStore.setState((state) => ({
    imagesById: {
      ...state.imagesById,
      [imageId]: image,
    },
  }));
}

function GenerationTileComponent({
  item,
  onOpen,
  selectionMode = false,
  selected = false,
  onToggleSelect,
  prewarmScheduler,
  fetchPriority = "low",
  imageSizes = "(max-width: 640px) 50vw, (max-width: 1024px) 33vw, 25vw",
}: GenerationTileProps) {
  const [tapped, setTapped] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [imageState, setImageState] = useState<{
    sourceVersion: string;
    failedSources: Set<string>;
    loaded: boolean;
  }>(() => ({
    sourceVersion: "",
    failedSources: new Set(),
    loaded: false,
  }));
  const pressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tapTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pressStart = useRef<{
    pointerId: number;
    x: number;
    y: number;
  } | null>(null);
  const suppressNextClick = useRef(false);
  const longPressed = useRef(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const hoverPrewarmRef = useRef<PrewarmHandle | null>(null);
  const router = useRouter();
  const promoteImageToReference = useChatStore(
    (state) => state.promoteImageToReference,
  );
  const model = useMemo(() => createGenerationTileModel(item), [item]);
  const currentImageState =
    imageState.sourceVersion === model.sourceVersion
      ? imageState
      : {
        sourceVersion: model.sourceVersion,
        failedSources: new Set<string>(),
        loaded: false,
      };
  const healthyCandidates = useMemo(
    () =>
      healthyAssetCandidates(
        model.gridCandidates,
        currentImageState.failedSources,
      ),
    [currentImageState.failedSources, model.gridCandidates],
  );
  const imageSrc = healthyCandidates[0]?.src ?? null;
  const imageSrcSet = assetCandidateSrcSet(healthyCandidates);
  const imageFailed = healthyCandidates.length === 0;
  const imageLoaded = currentImageState.loaded;

  useEffect(() => {
    return () => {
      if (pressTimer.current) clearTimeout(pressTimer.current);
      if (tapTimer.current) clearTimeout(tapTimer.current);
      hoverPrewarmRef.current?.cancel();
    };
  }, []);

  const clearPress = useCallback(() => {
    if (pressTimer.current) {
      clearTimeout(pressTimer.current);
      pressTimer.current = null;
    }
    pressStart.current = null;
  }, []);

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (selectionMode || !event.isPrimary || event.button !== 0) return;
      longPressed.current = false;
      suppressNextClick.current = false;
      pressStart.current = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
      };
      if (pressTimer.current) clearTimeout(pressTimer.current);
      pressTimer.current = setTimeout(() => {
        longPressed.current = true;
        setSheetOpen(true);
        try {
          navigator.vibrate?.(10);
        } catch {
          // Vibration is optional feedback.
        }
      }, LONG_PRESS_MS);
    },
    [selectionMode],
  );

  const onPreviewIntent = useCallback(() => {
    if (selectionMode) return;
    hoverPrewarmRef.current?.cancel();
    hoverPrewarmRef.current = prewarmScheduler.scheduleImages(
      model.hoverPrewarmSources,
      {
        priority: "hover",
        assetKind: "preview",
      },
      1,
    );
  }, [model.hoverPrewarmSources, prewarmScheduler, selectionMode]);

  const clearPreviewIntent = useCallback(() => {
    hoverPrewarmRef.current?.cancel();
    hoverPrewarmRef.current = null;
  }, []);

  const onPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const start = pressStart.current;
      if (!start || start.pointerId !== event.pointerId) return;
      const distance = Math.hypot(
        event.clientX - start.x,
        event.clientY - start.y,
      );
      if (distance <= PRESS_MOVE_SLOP_PX) return;
      suppressNextClick.current = true;
      clearPress();
    },
    [clearPress],
  );

  const onClick = useCallback(() => {
    if (suppressNextClick.current) {
      suppressNextClick.current = false;
      return;
    }
    if (longPressed.current) {
      longPressed.current = false;
      return;
    }
    if (selectionMode) {
      onToggleSelect?.(model.imageId);
      return;
    }

    setTapped(true);
    clearPreviewIntent();
    if (tapTimer.current) clearTimeout(tapTimer.current);
    tapTimer.current = setTimeout(() => setTapped(false), TAP_FEEDBACK_MS);
    const element = rootRef.current;
    if (!element) return;
    const image = element.querySelector("img");
    onOpen(item.id, (image ?? element).getBoundingClientRect());
  }, [
    item.id,
    model.imageId,
    clearPreviewIntent,
    onOpen,
    onToggleSelect,
    selectionMode,
  ]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (!isTileActivationKey(event.key)) return;
      event.preventDefault();
      onClick();
    },
    [onClick],
  );

  const onMakeRef = useCallback(() => {
    const before = useChatStore.getState().composer.attachments.length;
    try {
      ensureImageInChatStore(item);
      promoteImageToReference(model.imageId);
      const after = useChatStore.getState().composer.attachments.length;
      if (after <= before) throw new Error("image_not_available");
      router.push("/");
      pushMobileToast("已添加为参考图", "success");
    } catch {
      pushMobileToast("添加参考图失败", "danger");
    }
  }, [item, model.imageId, promoteImageToReference, router]);

  const onSave = useCallback(() => {
    const url = item.image.url;
    if (!url) {
      pushMobileToast("原图地址不可用", "danger");
      return;
    }
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.rel = "noopener noreferrer";
    anchor.target = "_blank";
    anchor.download = imageDownloadName(item);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }, [item]);

  const onCopyPrompt = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(item.prompt);
      pushMobileToast("已复制提示词", "success");
    } catch {
      pushMobileToast("复制失败", "danger");
    }
  }, [item.prompt]);

  const onLocate = useCallback(() => {
    const query = new URLSearchParams({
      conversationId: item.conversation_id,
      scrollTo: item.message_id,
    });
    router.push(`/?${query.toString()}`);
  }, [item.conversation_id, item.message_id, router]);

  const retryImage = useCallback(() => {
    setImageState({
      sourceVersion: model.sourceVersion,
      failedSources: new Set(),
      loaded: false,
    });
  }, [model.sourceVersion]);
  const onImageLoad = useCallback(() => {
    setImageState((current) => ({
      sourceVersion: model.sourceVersion,
      failedSources:
        current.sourceVersion === model.sourceVersion
          ? current.failedSources
          : new Set(),
      loaded: true,
    }));
  }, [model.sourceVersion]);
  const onImageError = useCallback((event: SyntheticEvent<HTMLImageElement>) => {
    const failedSource = failedAssetCandidateSource(
      healthyCandidates,
      event.currentTarget.currentSrc || event.currentTarget.src,
      document.baseURI,
    ) ?? imageSrc;
    if (!failedSource) return;
    setImageState((current) => {
      const next =
        current.sourceVersion === model.sourceVersion
          ? new Set(current.failedSources)
          : new Set<string>();
      next.add(failedSource);
      return {
        sourceVersion: model.sourceVersion,
        failedSources: next,
        loaded: false,
      };
    });
  }, [healthyCandidates, imageSrc, model.sourceVersion]);
  const closeSheet = useCallback(() => setSheetOpen(false), []);

  return (
    <>
      <article
        className={cn(
          "group relative block w-full overflow-hidden rounded-[var(--radius-card)]",
          "border border-[var(--border-subtle)] bg-[var(--bg-1)]/94 text-left shadow-[var(--shadow-1)]",
          "transition-[border-color,box-shadow,transform,background-color] duration-200 ease-[var(--ease-develop)]",
          "hover:-translate-y-0.5 hover:border-[var(--border-strong)] hover:bg-[var(--bg-1)] hover:shadow-[var(--shadow-2)]",
          "active:scale-[0.995]",
          tapped && "shadow-amber",
          selected && "border-[var(--amber-400)] shadow-amber",
        )}
      >
        <div
          ref={rootRef}
          role="button"
          tabIndex={0}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerEnter={onPreviewIntent}
          onPointerUp={clearPress}
          onPointerLeave={() => {
            clearPress();
            clearPreviewIntent();
          }}
          onPointerCancel={clearPress}
          onClick={onClick}
          onFocus={onPreviewIntent}
          onBlur={clearPreviewIntent}
          onKeyDown={onKeyDown}
          className="block w-full cursor-pointer text-left focus-visible:outline-none"
          aria-label={model.promptShort || "查看作品"}
          aria-pressed={selectionMode ? selected : undefined}
        >
          <GenerationTileMedia
            model={model}
            imageSrc={imageSrc}
            imageSrcSet={imageSrcSet}
            imageFailed={imageFailed}
            imageLoaded={imageLoaded}
            selectionMode={selectionMode}
            selected={selected}
            onLoad={onImageLoad}
            onError={onImageError}
            onRetry={retryImage}
            fetchPriority={fetchPriority}
            imageSizes={imageSizes}
          />
          <GenerationTileMetadata item={item} model={model} />
        </div>

        <GenerationTileDesktopActions
          imageFailed={imageFailed}
          selectionMode={selectionMode}
          onMakeRef={onMakeRef}
          onCopyPrompt={onCopyPrompt}
          onSave={onSave}
          onRetry={retryImage}
        />
      </article>

      <GenerationTileActionSheet
        open={sheetOpen}
        onClose={closeSheet}
        onMakeRef={onMakeRef}
        onSave={onSave}
        onCopyPrompt={onCopyPrompt}
        onLocate={onLocate}
      />
    </>
  );
}

function GenerationTileMedia({
  model,
  imageSrc,
  imageSrcSet,
  imageFailed,
  imageLoaded,
  selectionMode,
  selected,
  onLoad,
  onError,
  onRetry,
  fetchPriority,
  imageSizes,
}: {
  model: GenerationTileModel;
  imageSrc: string | null;
  imageSrcSet: string | undefined;
  imageFailed: boolean;
  imageLoaded: boolean;
  selectionMode: boolean;
  selected: boolean;
  onLoad: () => void;
  onError: (event: SyntheticEvent<HTMLImageElement>) => void;
  onRetry: () => void;
  fetchPriority: "high" | "low" | "auto";
  imageSizes: string;
}) {
  return (
    <div
      className="relative w-full overflow-hidden bg-[var(--bg-2)]"
      style={{ aspectRatio: `${model.width} / ${model.height}` }}
    >
      {!imageLoaded && !imageFailed && (
        <div className="absolute inset-0 animate-shimmer bg-[var(--bg-2)]" />
      )}
      {imageSrc && !imageFailed ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={imageSrc}
          srcSet={imageSrcSet}
          sizes={imageSizes}
          alt={model.altText}
          loading="lazy"
          decoding="async"
          fetchPriority={fetchPriority}
          draggable={false}
          onLoad={onLoad}
          onError={onError}
          className={cn(
            "absolute inset-0 h-full w-full object-cover transition-[transform,opacity,filter] duration-300 ease-[var(--ease-develop)]",
            "group-hover:scale-[1.025] group-hover:brightness-[1.04]",
            imageLoaded ? "opacity-100" : "opacity-0",
          )}
        />
      ) : (
        <ImageLoadFailure onRetry={onRetry} />
      )}
      <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-[var(--media-control-bg)] via-transparent to-[var(--surface-scrim)] opacity-0 transition-opacity duration-200 group-hover:opacity-100" />
      {/* @ui-governance-allow media: age metadata overlays the generated asset. */}
      {model.age && (
        <span className="pointer-events-none absolute left-2 top-2 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-1.5 py-1 type-caption tabular-nums text-[var(--media-control-fg)] opacity-0 backdrop-blur-md transition-opacity duration-200 group-hover:opacity-100">
          {model.age}
        </span>
      )}
      {selectionMode && (
        <span
          className={cn(
            // @ui-governance-allow media: selection control overlays the asset thumbnail.
            "absolute right-2 top-2 inline-flex h-11 w-11 items-center justify-center rounded-full border backdrop-blur-md transition-colors md:h-8 md:w-8",
            selected
              ? "border-accent-border bg-[var(--accent)] text-[var(--accent-on)]"
              : "border-[var(--border-strong)] bg-[var(--media-control-bg)] text-[var(--media-control-fg)]",
          )}
          aria-hidden
        >
          {selected && <Check className="h-4 w-4" />}
        </span>
      )}
    </div>
  );
}

function ImageLoadFailure({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-[var(--bg-2)] text-[var(--fg-2)]">
      <ImageIcon className="h-5 w-5" />
      <span className="type-caption">图片载入失败</span>
      <button
        type="button"
        onPointerDown={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}
        onClick={(event) => {
          event.stopPropagation();
          onRetry();
        }}
        className="mt-1 inline-flex min-h-11 cursor-pointer items-center gap-1 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] px-3 type-caption text-[var(--fg-1)] hover:text-[var(--fg-0)] md:min-h-8"
      >
        <RotateCcw className="h-3 w-3" />
        重试
      </button>
    </div>
  );
}

function GenerationTileMetadata({
  item,
  model,
}: {
  item: GenerationSummary;
  model: GenerationTileModel;
}) {
  return (
    <div className="space-y-2 px-2.5 pb-2.5 pt-2.5">
      <div
        className="type-body-sm leading-[1.5] text-[var(--fg-0)]"
        style={{
          display: "-webkit-box",
          WebkitLineClamp: 2,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}
      >
        {model.promptShort}
        {model.promptTruncated && "…"}
      </div>
      <div className="flex min-w-0 items-center gap-1.5 overflow-hidden">
        {model.age && (
          <span className="shrink-0 type-caption tabular-nums text-[var(--fg-2)]">
            {model.age}
          </span>
        )}
        {item.aspect_ratio && (
          <span className="shrink-0 rounded-[4px] border border-[var(--border-subtle)] bg-[var(--bg-2)] px-1.5 py-px type-caption font-mono tabular-nums text-[var(--fg-2)]">
            {item.aspect_ratio}
          </span>
        )}
        {item.has_ref && (
          <span className="shrink-0 rounded-[4px] border border-[rgba(139,92,246,0.18)] bg-[rgba(139,92,246,0.12)] px-1.5 py-px type-caption font-medium text-[#a78bfa]">
            参考图
          </span>
        )}
      </div>
    </div>
  );
}

function GenerationTileDesktopActions({
  imageFailed,
  selectionMode,
  onMakeRef,
  onCopyPrompt,
  onSave,
  onRetry,
}: {
  imageFailed: boolean;
  selectionMode: boolean;
  onMakeRef: () => void;
  onCopyPrompt: () => void;
  onSave: () => void;
  onRetry: () => void;
}) {
  return (
    <div
      className={cn(
        "pointer-events-none absolute right-2 top-2 hidden items-center gap-1 opacity-0 transition-opacity duration-200 group-hover:opacity-100 md:flex",
        selectionMode && "hidden",
      )}
    >
      <TileAction label="做参考图" onClick={onMakeRef}>
        <ImageIcon className="h-3.5 w-3.5" />
      </TileAction>
      <TileAction label="复制提示词" onClick={onCopyPrompt}>
        <Copy className="h-3.5 w-3.5" />
      </TileAction>
      <TileAction label="保存原图" onClick={onSave}>
        <Download className="h-3.5 w-3.5" />
      </TileAction>
      {imageFailed && (
        <TileAction label="重试载入" onClick={onRetry}>
          <RotateCcw className="h-3.5 w-3.5" />
        </TileAction>
      )}
    </div>
  );
}

function GenerationTileActionSheet({
  open,
  onClose,
  onMakeRef,
  onSave,
  onCopyPrompt,
  onLocate,
}: {
  open: boolean;
  onClose: () => void;
  onMakeRef: () => void;
  onSave: () => void;
  onCopyPrompt: () => void;
  onLocate: () => void;
}) {
  return (
    <ActionSheet
      open={open}
      onClose={onClose}
      actions={[
        {
          key: "ref",
          label: "做参考图",
          icon: <ImageIcon className="h-4 w-4" />,
          onSelect: onMakeRef,
        },
        {
          key: "save",
          label: "保存到相册",
          icon: <ImageDown className="h-4 w-4" />,
          onSelect: onSave,
        },
        {
          key: "copy",
          label: "复制提示词",
          icon: <Copy className="h-4 w-4" />,
          onSelect: onCopyPrompt,
        },
        {
          key: "locate",
          label: "在对话中定位",
          icon: <Crosshair className="h-4 w-4" />,
          onSelect: onLocate,
        },
      ]}
    />
  );
}

export const GenerationTile = memo(GenerationTileComponent);

function TileAction({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      className={cn(
        // @ui-governance-allow media: quick action overlays the generated asset.
        "pointer-events-auto inline-flex h-11 w-11 cursor-pointer items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--media-control-bg)] text-[var(--media-control-fg)] shadow-[var(--shadow-2)] backdrop-blur-md transition-[filter,transform] hover:brightness-110 active:scale-95 focus-visible:outline-none lg:h-9 lg:w-9",
      )}
    >
      {children}
    </button>
  );
}
