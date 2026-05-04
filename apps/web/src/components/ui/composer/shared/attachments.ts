import type { AttachmentImage } from "@/lib/types";
import { MAX_COMPOSER_ATTACHMENTS } from "@/lib/attachmentLimits";

export { MAX_COMPOSER_ATTACHMENTS };

export function imageFilesFromList(files: Iterable<File>): File[] {
  return Array.from(files).filter((file) => file.type.startsWith("image/"));
}

export function imageFilesFromDataTransfer(
  dataTransfer: DataTransfer | null,
): File[] {
  if (!dataTransfer) return [];
  if (dataTransfer.items?.length) {
    const files: File[] = [];
    for (const item of Array.from(dataTransfer.items)) {
      if (item.kind !== "file") continue;
      const file = item.getAsFile();
      if (file && file.type.startsWith("image/")) files.push(file);
    }
    return files;
  }
  return imageFilesFromList(dataTransfer.files ?? []);
}

export function hasImageFile(dataTransfer: DataTransfer | null): boolean {
  if (!dataTransfer) return false;
  if (dataTransfer.items?.length) {
    return Array.from(dataTransfer.items).some(
      (item) =>
        item.kind === "file" &&
        (!item.type || item.type.startsWith("image/")),
    );
  }
  if (Array.from(dataTransfer.types ?? []).includes("Files")) return true;
  return imageFilesFromList(dataTransfer.files ?? []).length > 0;
}

export function remainingAttachmentSlots(
  attachments: AttachmentImage[],
): number {
  return Math.max(0, MAX_COMPOSER_ATTACHMENTS - attachments.length);
}
