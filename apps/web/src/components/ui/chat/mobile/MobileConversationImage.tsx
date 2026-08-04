"use client";

import { memo } from "react";

import { pushMobileToast } from "@/components/ui/primitives/mobile";
import {
  FinalImage as ConversationFinalImage,
  lightboxItemForConversationImage,
} from "@/components/ui/chat/ConversationVisualAtoms";
import { tryCopyTextToClipboard } from "@/lib/clipboard";
import type { Generation, GeneratedImage } from "@/lib/types";
import type { LightboxItem } from "@/components/ui/lightbox/types";

function openLightbox(
  items: LightboxItem[],
  initialId: string,
  fromRect: DOMRect | null,
) {
  if (typeof window === "undefined" || items.length === 0) return;
  window.dispatchEvent(
    new CustomEvent("lumen:open-lightbox", {
      detail: { items, initialId, fromRect: fromRect ?? undefined },
    }),
  );
}

interface FinalImageProps {
  gen: Generation;
  image: GeneratedImage;
  onEditImage: (id: string) => void;
  inGrid?: boolean;
}

export const FinalImage = memo(function FinalImage({
  gen,
  image,
  onEditImage,
  inGrid = false,
}: FinalImageProps) {
  const handleCopy = () => {
    void tryCopyTextToClipboard(gen.prompt).then((success) => {
      pushMobileToast(
        success ? "已复制 prompt" : "复制失败",
        success ? "success" : "danger",
      );
    });
  };

  const handlePreview = (button: HTMLButtonElement | null) => {
    const item = lightboxItemForConversationImage(gen, image);
    openLightbox(
      [item],
      image.id,
      button?.getBoundingClientRect() ?? null,
    );
  };

  return (
    <ConversationFinalImage
      gen={gen}
      image={image}
      platform="mobile"
      inGrid={inGrid}
      onPreview={handlePreview}
      onCopy={handleCopy}
      onEditImage={() => onEditImage(image.id)}
    />
  );
});
