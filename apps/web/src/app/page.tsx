"use client";

// 顶层分流：< 768px → MobileStudio；≥ 768px → DesktopStudio（原桌面逻辑不动）。
// useMediaQuery 首次返回 null，渲染 <ShellSkeleton/> 避免 hydration mismatch。

import { useIsMobile } from "@/hooks/useMediaQuery";
import { ShellSkeleton } from "@/components/ui/shell/ShellSkeleton";
import { MobileStudio } from "@/components/ui/shell/MobileStudio";
import { DesktopStudio } from "@/components/ui/shell/DesktopStudio";

export default function Page() {
  const isMobile = useIsMobile();
  if (isMobile === null) return <ShellSkeleton />;
  return isMobile ? <MobileStudio /> : <DesktopStudio />;
}
