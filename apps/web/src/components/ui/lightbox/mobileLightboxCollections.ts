import type {
  ThumbnailItem,
  VisibleSlide,
} from "./MobileLightboxView";
import type { LightboxItem } from "./types";

const THUMB_WINDOW_SIZE = 17;

export function mobileLightboxThumbnailItems(
  items: LightboxItem[],
  idx: number,
  total: number,
): ThumbnailItem[] {
  if (idx < 0 || total <= THUMB_WINDOW_SIZE) {
    return items.map((item, itemIdx) => ({ item, itemIdx }));
  }
  const radius = Math.floor(THUMB_WINDOW_SIZE / 2);
  let start = Math.max(0, idx - radius);
  const end = Math.min(total, start + THUMB_WINDOW_SIZE);
  start = Math.max(0, end - THUMB_WINDOW_SIZE);
  return items
    .slice(start, end)
    .map((item, offset) => ({ item, itemIdx: start + offset }));
}

export function mobileLightboxVisibleSlides(
  items: LightboxItem[],
  current: LightboxItem | null,
  idx: number,
  total: number,
): VisibleSlide[] {
  if (!current || idx < 0) return [];
  const slides: VisibleSlide[] = [];
  if (idx > 0) slides.push({ item: items[idx - 1], offset: -1 });
  slides.push({ item: current, offset: 0 });
  if (idx < total - 1) slides.push({ item: items[idx + 1], offset: 1 });
  return slides;
}
