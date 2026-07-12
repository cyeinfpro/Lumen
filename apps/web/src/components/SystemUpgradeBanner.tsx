"use client";

import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useEffect, useRef } from "react";

import { getSystemMaintenance } from "@/lib/apiClient";

export function SystemUpgradeBanner() {
  const ref = useRef<HTMLDivElement>(null);
  const q = useQuery({
    queryKey: ["system", "maintenance"],
    queryFn: getSystemMaintenance,
    refetchInterval: (query) => {
      if (query.state.error) return 30_000;
      return query.state.data?.running ? 5000 : 60000;
    },
    retry: 2,
  });
  const data = q.data;
  const running = Boolean(data?.running);

  useEffect(() => {
    if (!running) {
      document.documentElement.style.setProperty("--system-banner-height", "0px");
      return;
    }
    const el = ref.current;
    if (!el) return;
    const update = () => {
      document.documentElement.style.setProperty("--system-banner-height", `${el.offsetHeight}px`);
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => {
      ro.disconnect();
      document.documentElement.style.setProperty("--system-banner-height", "0px");
    };
  }, [running]);

  if (!data?.running) return null;
  const target = data.target_tag ? `到 ${data.target_tag}` : "";
  const phase = data.phase ?? "preparing";
  const eta = Math.max(1, data.estimated_remaining_min ?? 1);

  return (
    <div
      ref={ref}
      role="status"
      aria-live="polite"
      data-system-banner
      className="fixed inset-x-0 top-0 border-b border-warning-border bg-warning-soft text-sm text-[var(--warning-fg)] shadow-[var(--shadow-2)] backdrop-blur-md"
      style={{
        zIndex: "var(--z-banner, 85)",
        paddingTop: "env(safe-area-inset-top, 0px)",
      }}
    >
      <div
        className="flex min-h-11 items-start justify-center gap-2 px-4 py-2 sm:items-center"
      >
        <RefreshCw
          className="mt-0.5 h-4 w-4 shrink-0 animate-spin sm:mt-0"
          aria-hidden
        />
        <span className="max-w-[min(92vw,640px)] break-words text-left sm:text-center">
          Lumen 正在升级{target}（{phase} · 预计 {eta} 分钟内完成），请求会自动重试。
        </span>
      </div>
    </div>
  );
}
