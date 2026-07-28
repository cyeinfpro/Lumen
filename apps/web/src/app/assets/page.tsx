"use client";

import { useIsMobile } from "@/hooks/useMediaQuery";
import { ShellSkeleton } from "@/components/ui/shell/ShellSkeleton";
import { DesktopStream, MobileStream } from "@/features/assets";

export default function AssetsPage() {
  const isMobile = useIsMobile();
  if (isMobile === null) return <ShellSkeleton />;
  return isMobile ? <MobileStream /> : <DesktopStream />;
}
