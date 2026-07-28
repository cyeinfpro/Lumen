"use client";

import { RuntimeResilienceStatus } from "@/components/RuntimeResilienceStatus";
import { useLumenRealtime } from "@/features/realtime";

export function SSEProvider({ children }: { children: React.ReactNode }) {
  useLumenRealtime();
  return (
    <>
      {children}
      <RuntimeResilienceStatus />
    </>
  );
}
