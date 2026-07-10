"use client";

import { useIsMobile } from "@/hooks/useMediaQuery";
import { DesktopStudio } from "./DesktopStudio";
import { MobileStudio } from "./MobileStudio";

export function ResponsiveStudio({
  initialMobile,
}: {
  initialMobile: boolean;
}) {
  const detectedMobile = useIsMobile();
  const mobile = detectedMobile ?? initialMobile;

  return mobile ? <MobileStudio /> : <DesktopStudio />;
}
