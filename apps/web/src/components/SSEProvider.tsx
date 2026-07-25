"use client";

import { RuntimeResilienceStatus } from "@/components/RuntimeResilienceStatus";
import { useLumenRealtime } from "@/lib/sse/useLumenRealtime";

export function SSEProvider({ children }: { children: React.ReactNode }) {
  useLumenRealtime();
  return (
    <>
      {children}
      <RuntimeResilienceStatus />
    </>
  );
}
