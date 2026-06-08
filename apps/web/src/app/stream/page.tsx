"use client";

// /stream 路由分流：
// - < 768px → MobileStream（资产瀑布流）
// - ≥ 768px → DesktopStream（桌面资产外壳）

import { useIsMobile } from "@/hooks/useMediaQuery";
import { ShellSkeleton } from "@/components/ui/shell/ShellSkeleton";
import dynamic from "next/dynamic";

const MobileStream = dynamic(
  () =>
    import("@/components/ui/shell/MobileStream").then((mod) => mod.MobileStream),
  { ssr: false, loading: () => <ShellSkeleton /> },
);

const DesktopStream = dynamic(
  () =>
    import("@/components/ui/shell/DesktopStream").then((mod) => mod.DesktopStream),
  { ssr: false, loading: () => <ShellSkeleton /> },
);

export default function StreamPage() {
  const isMobile = useIsMobile();
  if (isMobile === null) return <ShellSkeleton />;
  return isMobile ? <MobileStream /> : <DesktopStream />;
}
