"use client";

// /me 路由分流：
// - < 768px → MobileMe（账号 + 会话合体）
// - ≥ 768px → DesktopMe（桌面双栏外壳）

import { useIsMobile } from "@/hooks/useMediaQuery";
import { ShellSkeleton } from "@/components/ui/shell/ShellSkeleton";
import { MobileMe } from "@/components/ui/shell/MobileMe";
import { DesktopMe } from "@/components/ui/shell/DesktopMe";

export default function MePage() {
  const isMobile = useIsMobile();
  if (isMobile === null) return <ShellSkeleton />;
  return isMobile ? <MobileMe /> : <DesktopMe />;
}
