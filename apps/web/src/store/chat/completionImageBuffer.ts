import type { GeneratedImage } from "@/lib/types";

export const PENDING_COMPLETION_IMAGE_TTL_MS = 60_000;
const PENDING_COMPLETION_IMAGE_MAX_ENTRIES = 256;

export interface PendingCompletionImage {
  userScope: string;
  completionId: string;
  rawMessageId?: string;
  image: GeneratedImage;
  eventNow: number;
  expiresAt: number;
}

const pendingCompletionImages = new Map<string, PendingCompletionImage>();

function pendingCompletionImageKey(
  userScope: string,
  completionId: string,
): string {
  return `${userScope}\n${completionId}`;
}

function prunePendingCompletionImages(now: number): void {
  for (const [key, entry] of pendingCompletionImages) {
    if (entry.expiresAt > now) continue;
    pendingCompletionImages.delete(key);
  }
}

function prunePendingCompletionImagesToLimit(): void {
  while (pendingCompletionImages.size > PENDING_COMPLETION_IMAGE_MAX_ENTRIES) {
    const first = pendingCompletionImages.keys().next();
    if (first.done) return;
    pendingCompletionImages.delete(first.value);
  }
}

export function bufferPendingCompletionImage(
  entry: Omit<PendingCompletionImage, "expiresAt">,
  now = Date.now(),
): void {
  prunePendingCompletionImages(now);
  const key = pendingCompletionImageKey(entry.userScope, entry.completionId);
  pendingCompletionImages.delete(key);
  pendingCompletionImages.set(key, {
    ...entry,
    expiresAt: now + PENDING_COMPLETION_IMAGE_TTL_MS,
  });
  prunePendingCompletionImagesToLimit();
}

export function getPendingCompletionImage(
  userScope: string,
  completionId: string,
  now = Date.now(),
): PendingCompletionImage | undefined {
  prunePendingCompletionImages(now);
  return pendingCompletionImages.get(
    pendingCompletionImageKey(userScope, completionId),
  );
}

export function pendingCompletionImagesForScope(
  userScope: string,
  now = Date.now(),
): PendingCompletionImage[] {
  prunePendingCompletionImages(now);
  return Array.from(pendingCompletionImages.values()).filter(
    (entry) => entry.userScope === userScope,
  );
}

export function removePendingCompletionImage(
  userScope: string,
  completionId: string,
): void {
  pendingCompletionImages.delete(
    pendingCompletionImageKey(userScope, completionId),
  );
}

export function clearPendingCompletionImages(): void {
  pendingCompletionImages.clear();
}

export function pendingCompletionImageCount(): number {
  return pendingCompletionImages.size;
}
