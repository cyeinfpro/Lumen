import type { ReactNode } from "react";
import type { AttachmentImage } from "@/lib/types";

export function selectValue<T>(
  condition: boolean,
  whenTrue: T,
  whenFalse: T,
): T {
  return condition ? whenTrue : whenFalse;
}

export function renderWhen(
  condition: boolean,
  content: ReactNode,
): ReactNode {
  return condition ? content : null;
}

export function coalesceValue<T>(
  value: T | null | undefined,
  fallback: T,
): T {
  return value ?? fallback;
}

export function fallbackText(value: string, fallback: string): string {
  return value || fallback;
}

export function anyFlag(...flags: boolean[]): boolean {
  return flags.some(Boolean);
}

export function allFlags(...flags: boolean[]): boolean {
  return flags.every(Boolean);
}

export function firstAttachmentId(
  attachments: AttachmentImage[],
): string | null {
  return attachments[0]?.id ?? null;
}
