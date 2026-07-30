import type { MotionValue } from "framer-motion";
import type { Dispatch, RefObject, SetStateAction } from "react";
import type { LightboxAction } from "@/store/useUiStore";
import type { LightboxItem } from "./types";

export type ImgStatus = "loading" | "loaded" | "error";
export type DownloadStatus = "idle" | "downloading" | "success" | "error";
export type ActionNotice = {
  kind: "success" | "error" | "info";
  text: string;
} | null;
export type VisibleSlide = {
  item: LightboxItem;
  offset: -1 | 0 | 1;
};
export type ThumbnailItem = {
  item: LightboxItem;
  itemIdx: number;
};

export interface MobileLightboxViewProps {
  current: LightboxItem | null;
  idx: number;
  total: number;
  isFirst: boolean;
  isLast: boolean;
  paramsOpen: boolean;
  imgStatus: ImgStatus;
  useFallback: boolean;
  fallbackItemIds: ReadonlySet<string>;
  chromeVisible: boolean;
  zoomLevel: number;
  downloadStatus: DownloadStatus;
  actionNotice: ActionNotice;
  boundaryHint: "first" | "last" | null;
  lightboxAction: LightboxAction | null;
  visibleSlides: VisibleSlide[];
  thumbItems: ThumbnailItem[];
  gestureTargetRef: RefObject<HTMLDivElement | null>;
  downloadAnchorRef: RefObject<HTMLAnchorElement | null>;
  dialogRootRef: RefObject<HTMLDivElement | null>;
  closeButtonRef: RefObject<HTMLButtonElement | null>;
  activeThumbRef: RefObject<HTMLButtonElement | null>;
  dialogTitleId: string;
  dragX: MotionValue<number>;
  dragY: MotionValue<number>;
  scale: MotionValue<number>;
  haloOpacity: MotionValue<number>;
  onClose: () => void;
  onGoto: (delta: 1 | -1) => void;
  onResetZoom: () => void;
  onDownload: () => void;
  onSwitchItem: (item: LightboxItem) => void;
  onMarkFallback: (id: string) => void;
  setUseFallback: Dispatch<SetStateAction<boolean>>;
  setImgStatus: Dispatch<SetStateAction<ImgStatus>>;
  onIterate: () => void;
  onInpaint: () => void;
  onUpscale: () => void;
  onReroll: () => void;
  onCopyPrompt: () => void;
  onShare: () => void;
  onOpenParams: () => void;
  onCloseParams: () => void;
}
