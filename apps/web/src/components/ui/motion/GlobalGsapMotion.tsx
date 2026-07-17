import type { ReactNode } from "react";

export function GlobalGsapMotion({ children }: { children: ReactNode }) {
  return (
    <div data-lumen-motion-root className="contents">
      {children}
    </div>
  );
}
