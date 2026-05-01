"use client";

// 基础转圈占位。包装 lucide-react Loader2，提供统一 size 语义（12/16/20/24 px）。

import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

type SpinnerSize = 12 | 16 | 20 | 24;

interface SpinnerProps extends React.SVGAttributes<SVGSVGElement> {
  size?: SpinnerSize;
}

export function Spinner({
  size = 16,
  className,
  ref,
  ...props
}: SpinnerProps & { ref?: React.Ref<SVGSVGElement> }) {
  return (
    <Loader2
      ref={ref}
      width={size}
      height={size}
      aria-hidden="true"
      className={cn("animate-spin shrink-0", className)}
      {...props}
    />
  );
}

export default Spinner;
