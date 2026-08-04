"use client";

import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useEffect, useRef } from "react";

import { getSystemMaintenance } from "@/lib/apiClient";

const MAINTENANCE_PHASE_LABELS: Record<string, string> = {
  lock: "获取更新锁",
  self_update_scripts: "刷新更新脚本",
  check: "检查版本",
  preflight: "预检查",
  backup_preflight: "更新前备份",
  fetch_release: "准备发布目录",
  set_image_tag: "写入镜像标签",
  pull_images: "拉取镜像",
  start_infra: "启动基础设施",
  migrate_db: "迁移数据库",
  switch: "切换版本",
  check_storage: "检查存储",
  restart_services: "重启服务",
  refresh_update_runner: "刷新更新入口",
  health_check: "健康检查",
  health_post: "健康检查",
  cleanup: "清理旧版本",
  rollback: "回滚",
  preparing: "准备更新",
};

function maintenancePhaseLabel(phase?: string | null): string {
  if (!phase) return MAINTENANCE_PHASE_LABELS.preparing;
  return MAINTENANCE_PHASE_LABELS[phase] ?? "更新处理中";
}

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
  const target = data.target_tag ? `，目标版本 ${data.target_tag}` : "";
  const phase = maintenancePhaseLabel(data.phase);
  const eta = Math.max(1, data.estimated_remaining_min ?? 1);

  return (
    <div
      ref={ref}
      role="status"
      aria-live="polite"
      data-system-banner
      className="fixed inset-x-0 top-0 border-b border-warning-border bg-warning-soft type-body-sm text-[var(--warning-fg)] shadow-[var(--shadow-2)] backdrop-blur-md"
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
          Lumen 系统升级中{target}（{phase} · 预计 {eta} 分钟内完成），请求会自动重试。
        </span>
      </div>
    </div>
  );
}
