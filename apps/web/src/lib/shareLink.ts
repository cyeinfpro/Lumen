"use client";

import { copyTextToClipboard } from "./clipboard";

export async function shareOrCopyLink(
  url: string,
  title = "Lumen 分享",
): Promise<"shared" | "copied" | "failed" | "cancelled"> {
  if (typeof navigator !== "undefined" && typeof navigator.share === "function") {
    try {
      await navigator.share({ title, url });
      return "shared";
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        return "cancelled";
      }
    }
  }

  try {
    await copyTextToClipboard(url);
    return "copied";
  } catch {
    return "failed";
  }
}
