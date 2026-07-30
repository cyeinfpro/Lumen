import { AlertCircle, Check, Download, Loader2, Share2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { DownloadStatus, ShareStatus } from "./desktopLightboxModel";

export function DownloadStatusIcon({
  status,
  className = "h-4 w-4",
}: {
  status: DownloadStatus;
  className?: string;
}) {
  if (status === "downloading") {
    return <Loader2 className={cn(className, "animate-spin")} aria-hidden />;
  }
  if (status === "success") {
    return <Check className={className} aria-hidden />;
  }
  if (status === "error") {
    return <AlertCircle className={className} aria-hidden />;
  }
  return <Download className={className} aria-hidden />;
}

export function ShareStatusIcon({
  status,
}: {
  status: ShareStatus;
}) {
  if (status === "creating") {
    return <Loader2 className="h-4 w-4 animate-spin" aria-hidden />;
  }
  if (status === "success") {
    return <Check className="h-4 w-4" aria-hidden />;
  }
  if (status === "error") {
    return <AlertCircle className="h-4 w-4" aria-hidden />;
  }
  return <Share2 className="h-4 w-4" aria-hidden />;
}
