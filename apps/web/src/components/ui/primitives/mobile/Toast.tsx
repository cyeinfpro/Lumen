"use client";

import type { ReactNode } from "react";
import { toast } from "../Toast";

export type MobileToastKind = "info" | "success" | "warning" | "danger";

/**
 * Compatibility bridge for existing mobile call sites.
 * Rendering is handled by the single global ToastViewport.
 */
export function pushMobileToast(
  message: ReactNode,
  kind: MobileToastKind = "info",
) {
  const title = typeof message === "string" ? message : "操作已更新";
  if (kind === "danger") toast.error(title);
  else if (kind === "warning") toast.warning(title);
  else if (kind === "success") toast.success(title);
  else toast.info(title);
}

/** @deprecated The application now mounts only the global ToastViewport. */
export function MobileToastViewport() {
  return null;
}
