"use client";

import {
  useEffect,
  useMemo,
  useState,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from "react";

import {
  getPrivateIdentitySnapshot,
  isPrivateIdentitySnapshotCurrent,
  type PrivateIdentitySnapshot,
} from "@/lib/auth/privateIdentityEpoch";
import { useChatStore } from "@/store/useChatStore";

import {
  EMPTY_DESKTOP_GALLERY,
  toDesktopGalleryItem,
  type DesktopGalleryItem,
  type DesktopImageMeta,
} from "./desktopLightboxModel";
import {
  CLOSE_EVENT,
  OPEN_EVENT,
  type LightboxItem,
  type OpenLightboxDetail,
} from "./types";

type Generations = ReturnType<typeof useChatStore.getState>["generations"];

export function posterSource(
  meta: DesktopImageMeta | null,
): string | null {
  if (!meta) return null;
  return meta.thumb_url ?? meta.preview_url ?? null;
}

export function lightboxIdentity(lightbox: {
  ownerUserId: string | null;
  identityEpoch: number;
}): PrivateIdentitySnapshot {
  return {
    userId: lightbox.ownerUserId,
    epoch: lightbox.identityEpoch,
  };
}

interface GalleryStateArgs {
  open: boolean;
  imageId: string | null;
  storeEventItems: LightboxItem[] | null;
  generations: Generations;
}

export function useDesktopLightboxGallery({
  open,
  imageId,
  storeEventItems,
  generations,
}: GalleryStateArgs) {
  const [eventGallery, setEventGallery] = useState<
    DesktopGalleryItem[] | null
  >(null);
  const [eventItems, setEventItems] = useState<LightboxItem[] | null>(
    null,
  );
  const chatGallery = useMemo<DesktopGalleryItem[]>(() => {
    if (!open) return EMPTY_DESKTOP_GALLERY;
    const items = Object.values(generations).filter(
      (generation) =>
        generation.status === "succeeded" && generation.image,
    );
    items.sort((left, right) => left.started_at - right.started_at);
    return items.map((generation) => ({
      image: generation.image!,
      prompt: generation.prompt,
      started_at: generation.started_at,
    }));
  }, [generations, open]);
  const gallery = useMemo(() => {
    if (storeEventItems?.some((entry) => entry.id === imageId)) {
      return storeEventItems.map(toDesktopGalleryItem);
    }
    if (
      eventGallery?.some((entry) => entry.image.id === imageId)
    ) {
      return eventGallery;
    }
    return chatGallery;
  }, [chatGallery, eventGallery, imageId, storeEventItems]);

  return {
    eventItems,
    gallery,
    setEventGallery,
    setEventItems,
    clearEventGallery: () => {
      setEventGallery(null);
      setEventItems(null);
    },
  };
}

interface WindowEventsArgs {
  clearEdgeHintTimer: () => void;
  handleClose: () => void;
  openLightbox: (
    id: string,
    src: string,
    alt: string,
    previewSrc?: string,
  ) => void;
  preloadAbortRef: MutableRefObject<AbortController | null>;
  setEdgeHint: Dispatch<SetStateAction<"first" | "last" | null>>;
  setEventGallery: Dispatch<
    SetStateAction<DesktopGalleryItem[] | null>
  >;
  setEventItems: Dispatch<SetStateAction<LightboxItem[] | null>>;
  setPendingImageId: Dispatch<SetStateAction<string | null>>;
  setSlideDir: Dispatch<SetStateAction<1 | -1>>;
  switchSeqRef: MutableRefObject<number>;
}

export function useDesktopLightboxWindowEvents({
  clearEdgeHintTimer,
  handleClose,
  openLightbox,
  preloadAbortRef,
  setEdgeHint,
  setEventGallery,
  setEventItems,
  setPendingImageId,
  setSlideDir,
  switchSeqRef,
}: WindowEventsArgs) {
  useEffect(() => {
    const onOpen = (event: Event) => {
      const detail = (
        event as CustomEvent<OpenLightboxDetail>
      ).detail;
      if (!detail?.items?.length) return;
      const currentIdentity = getPrivateIdentitySnapshot();
      const identity =
        detail.ownerUserId && detail.identityEpoch !== undefined
          ? {
              userId: detail.ownerUserId,
              epoch: detail.identityEpoch,
            }
          : currentIdentity;
      if (!isPrivateIdentitySnapshotCurrent(identity)) return;
      const nextGallery = detail.items.map(toDesktopGalleryItem);
      const target =
        nextGallery.find(
          (entry) => entry.image.id === detail.initialId,
        ) ?? nextGallery[0];
      if (!target) return;

      switchSeqRef.current += 1;
      preloadAbortRef.current?.abort();
      preloadAbortRef.current = null;
      clearEdgeHintTimer();
      setEventGallery(nextGallery);
      setEventItems(detail.items);
      setSlideDir(1);
      setEdgeHint(null);
      setPendingImageId(null);
      if (detail.source === "store") return;
      openLightbox(
        target.image.id,
        target.image.data_url,
        target.prompt,
        target.image.preview_url ?? target.image.thumb_url,
      );
    };
    const onClose = () => handleClose();
    window.addEventListener(OPEN_EVENT, onOpen as EventListener);
    window.addEventListener(CLOSE_EVENT, onClose);
    return () => {
      window.removeEventListener(
        OPEN_EVENT,
        onOpen as EventListener,
      );
      window.removeEventListener(CLOSE_EVENT, onClose);
    };
  }, [
    clearEdgeHintTimer,
    handleClose,
    openLightbox,
    preloadAbortRef,
    setEdgeHint,
    setEventGallery,
    setEventItems,
    setPendingImageId,
    setSlideDir,
    switchSeqRef,
  ]);
}
