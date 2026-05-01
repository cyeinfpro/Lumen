"use client";

// /stream 路由分流：
// - < 768px → MobileStream（灵感流瀑布流）
// - ≥ 768px → DesktopStream（桌面灵感流外壳）

import { useIsMobile } from "@/hooks/useMediaQuery";
import { ShellSkeleton } from "@/components/ui/shell/ShellSkeleton";
import { MobileStream } from "@/components/ui/shell/MobileStream";
import { DesktopStream } from "@/components/ui/shell/DesktopStream";

export default function StreamPage() {
  const isMobile = useIsMobile();
  if (isMobile === null) return <ShellSkeleton />;
  return isMobile ? <MobileStream /> : <DesktopStream />;
}
