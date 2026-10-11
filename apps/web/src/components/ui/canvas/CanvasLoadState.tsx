"use client";

import type { ReactNode } from "react";
import { MobileRuntimeResilienceStatus } from "@/components/RuntimeResilienceStatus";

// Loading a document must not hide the action needed to recover its session.
// Desktop recovery remains owned by the global runtime status surface.
export function CanvasLoadState({ children }: { children: ReactNode }) {
  return (
    <div className="relative grid h-[100dvh] place-items-center bg-[var(--bg-0)] px-6">
      <div className="absolute right-3 top-[calc(env(safe-area-inset-top)+0.75rem)] md:hidden">
        <MobileRuntimeResilienceStatus />
      </div>
      {children}
    </div>
  );
}
