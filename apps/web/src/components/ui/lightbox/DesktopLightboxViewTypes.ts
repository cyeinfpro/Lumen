import type { PointerEventHandler, WheelEventHandler } from "react";
import type { DesktopGalleryItem, DownloadStatus, PanOffset, ShareStatus, ViewMode } from "./desktopLightboxModel";
import type { LightboxItem } from "./types";

export type ThumbnailItem = {
  entry: DesktopGalleryItem;
  index: number;
};

export type InjectedAction = {
  label: string;
  pending: boolean;
  onClick: () => void;
};

export type DesktopLightboxViewProps = {
  open: boolean;
  imageId: string | null | undefined;
  imageSrc: string | null | undefined;
  imageAlt: string | null | undefined;
  displaySrc: string | null | undefined;
  dialogTitleId: string;
  containerElementId: string;
  downloadAnchorElementId: string;
  imageWrapElementId: string;
  imageElementId: string;
  closeButtonElementId: string;
  galleryLength: number;
  currentIndex: number;
  hasPrevious: boolean;
  hasNext: boolean;
  thumbnails: ThumbnailItem[];
  posterSrc: string | null;
  sourceLabel: string;
  currentItem: LightboxItem | null;
  activeLoadError: boolean;
  activeViewMode: ViewMode;
  activeViewModeLabel: string;
  activeZoom: number;
  activePanOffset: PanOffset;
  isPanning: boolean;
  mainImageLoaded: boolean;
  detailsOpen: boolean;
  imageActionsAvailable: boolean;
  downloadStatus: DownloadStatus;
  downloadTitle: string;
  downloadText: string;
  shareStatus: ShareStatus;
  shareTitle: string;
  shareText: string;
  edgeHint: "first" | "last" | null;
  isSwitchingImage: boolean;
  injectedAction: InjectedAction | null;
  onWheel: WheelEventHandler<HTMLDivElement>;
  onBackdropMouseDown: React.MouseEventHandler<HTMLDivElement>;
  onBackdropMouseUp: React.MouseEventHandler<HTMLDivElement>;
  onClose: () => void;
  onZoomOut: () => void;
  onZoomIn: () => void;
  onResetView: () => void;
  onToggleDetails: () => void;
  onHideDetails: () => void;
  onIterate: () => void;
  onInpaint: () => void;
  onUpscale: () => void;
  onReroll: () => void;
  onDownload: () => void;
  onShare: () => void;
  onOpenOriginal: () => void;
  onPrevious: () => void;
  onNext: () => void;
  onImageLoad: () => void;
  onImageError: () => void;
  onImagePointerDown: PointerEventHandler<HTMLImageElement>;
  onImagePointerMove: PointerEventHandler<HTMLImageElement>;
  onImagePointerUp: PointerEventHandler<HTMLImageElement>;
  onImagePointerCancel: PointerEventHandler<HTMLImageElement>;
  onSelectThumbnail: (
    entry: DesktopGalleryItem,
    index: number,
  ) => void;
};
