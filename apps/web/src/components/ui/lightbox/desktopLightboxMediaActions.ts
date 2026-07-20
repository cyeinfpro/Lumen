import {
  downloadFilename,
  fetchImageBlob,
  type ShareStatus,
} from "./desktopLightboxModel";

type DownloadDesktopImageOptions = {
  src: string;
  id: string | null;
  mime?: string;
  filename?: string;
  anchor: HTMLAnchorElement;
  operationIsCurrent: () => boolean;
};

export async function downloadDesktopImage({
  src,
  id,
  mime,
  filename,
  anchor,
  operationIsCurrent,
}: DownloadDesktopImageOptions): Promise<"success" | "error" | null> {
  try {
    const blob = await fetchImageBlob(src);
    if (!operationIsCurrent()) return null;
    const objectUrl = URL.createObjectURL(blob);
    anchor.href = objectUrl;
    anchor.download = downloadFilename(
      id,
      src,
      blob.type || mime,
      filename,
    );
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    return "success";
  } catch {
    if (!operationIsCurrent()) return null;
    window.open(src, "_blank", "noopener,noreferrer");
    return "error";
  }
}

type ShareDesktopImageOptions = {
  imageId: string;
  createShare: (input: {
    imageId: string;
    show_prompt: boolean;
  }) => Promise<{ url: string }>;
  writeClipboard: (text: string) => Promise<void>;
  operationIsCurrent: () => boolean;
};

async function tryNativeShare(
  link: string,
): Promise<ShareStatus | null> {
  if (
    typeof navigator === "undefined" ||
    typeof navigator.share !== "function"
  ) {
    return null;
  }
  try {
    await navigator.share({
      title: "Lumen image",
      text: "Lumen image",
      url: link,
    });
    return "success";
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      return "idle";
    }
    return null;
  }
}

export async function shareDesktopImage({
  imageId,
  createShare,
  writeClipboard,
  operationIsCurrent,
}: ShareDesktopImageOptions): Promise<ShareStatus | null> {
  let link: string;
  try {
    const share = await createShare({
      imageId,
      show_prompt: false,
    });
    link = share.url;
  } catch {
    return operationIsCurrent() ? "error" : null;
  }
  if (!operationIsCurrent()) return null;

  const nativeResult = await tryNativeShare(link);
  if (!operationIsCurrent()) return null;
  if (nativeResult) return nativeResult;

  try {
    await writeClipboard(link);
    return operationIsCurrent() ? "success" : null;
  } catch {
    return operationIsCurrent() ? "error" : null;
  }
}
