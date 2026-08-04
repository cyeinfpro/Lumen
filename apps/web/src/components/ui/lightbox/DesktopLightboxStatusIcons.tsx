import { AlertCircle, Check, Download, Share2 } from "lucide-react";
import { Spinner } from "@/components/ui/primitives/Spinner";
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
    return <Spinner size={16} className={cn(className)} />;
  }
  if (status === "success") {
    return <Check className={className} aria-hidden />;
  }
  if (status === "error") {
    return <AlertCircle className={className} aria-hidden />;
  }
  return <Download className={className} aria-hidden />;
}

export function ShareStatusIcon({ status }: { status: ShareStatus }) {
  if (status === "creating") {
    return <Spinner size={16} />;
  }
  if (status === "success") {
    return <Check className="h-4 w-4" aria-hidden />;
  }
  if (status === "error") {
    return <AlertCircle className="h-4 w-4" aria-hidden />;
  }
  return <Share2 className="h-4 w-4" aria-hidden />;
}
