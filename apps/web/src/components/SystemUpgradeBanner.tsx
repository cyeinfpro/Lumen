"use client";

import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";

import { getSystemMaintenance } from "@/lib/apiClient";

export function SystemUpgradeBanner() {
  const q = useQuery({
    queryKey: ["system", "maintenance"],
    queryFn: getSystemMaintenance,
    refetchInterval: (query) => (query.state.data?.running ? 5000 : 15000),
    retry: false,
  });
  const data = q.data;
  if (!data?.running) return null;
  const target = data.target_tag ? `到 ${data.target_tag}` : "";
  const phase = data.phase ?? "preparing";
  const eta = Math.max(1, data.estimated_remaining_min ?? 1);

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed inset-x-0 top-0 z-[90] flex items-center justify-center gap-2 border-b border-amber-400/25 bg-amber-500/18 px-4 py-2 text-center text-sm text-amber-100 shadow-[var(--shadow-2)] backdrop-blur-md"
    >
      <RefreshCw className="h-4 w-4 animate-spin" aria-hidden />
      <span>
        Lumen 正在升级{target}（{phase} · 预计 {eta} 分钟内完成），请求会自动重试。
      </span>
    </div>
  );
}
