"use client";

import { useIsMobile } from "@/hooks/useMediaQuery";
import { ShellSkeleton } from "@/components/ui/shell/ShellSkeleton";
import { MobileStream } from "@/components/ui/shell/MobileStream";
import { DesktopStream } from "@/components/ui/shell/DesktopStream";

export default function AssetsPage() {
  const isMobile = useIsMobile();
  if (isMobile === null) return <ShellSkeleton />;
  return isMobile ? <MobileStream /> : <DesktopStream />;
}
