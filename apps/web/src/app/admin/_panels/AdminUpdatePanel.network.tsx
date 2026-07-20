"use client";

import { useMemo } from "react";
import { Loader2, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { cn } from "@/lib/utils";

const UPDATE_USE_PROXY_POOL_KEY = "update.use_proxy_pool";
const UPDATE_PROXY_NAME_KEY = "update.proxy_name";

interface UpdateNetworkSettingsCardProps {
  settings: Array<{
    key: string;
    value: string | null;
    has_value?: boolean;
  }>;
  proxies: Array<{
    name: string;
    enabled: boolean;
    in_cooldown: boolean;
    last_latency_ms: number | null;
  }>;
  loading: boolean;
  saving: boolean;
  error: Error | null;
  onRetry: () => void;
  onSave: (items: { key: string; value: string }[]) => void;
}

function shouldShowLoading(error: Error | null, loading: boolean): boolean {
  return !error && loading;
}

export function UpdateNetworkSettingsCard({
  settings,
  proxies,
  loading,
  saving,
  error,
  onRetry,
  onSave,
}: UpdateNetworkSettingsCardProps) {
  const settingMap = useMemo(
    () => new Map(settings.map((item) => [item.key, item])),
    [settings],
  );
  const useProxyPool =
    (settingMap.get(UPDATE_USE_PROXY_POOL_KEY)?.value ?? "0") === "1";
  const proxyName = settingMap.get(UPDATE_PROXY_NAME_KEY)?.value ?? "";
  const enabledProxies = proxies.filter((proxy) => proxy.enabled);

  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)] backdrop-blur-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <p className="type-card-title">更新网络设置</p>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            控制一键更新拉取代码、依赖和镜像时是否走代理池。
          </p>
        </div>
        <div>
          {error && (
            <div role="alert">
              <Button
                variant="secondary"
                size="sm"
                onClick={onRetry}
                leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
              >
                {copy.action.retry}
              </Button>
            </div>
          )}
          {shouldShowLoading(error, loading) && (
            <span
              role="status"
              className="inline-flex items-center gap-1.5 type-caption text-[var(--fg-2)]"
            >
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              读取中
            </span>
          )}
        </div>
      </div>

      {error ? (
        <p role="alert" className="mt-3 type-caption text-danger">
          读取更新网络设置失败：{error.message}
        </p>
      ) : (
        <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
          <div className="flex flex-wrap items-center gap-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-3">
            <button
              type="button"
              role="switch"
              aria-checked={useProxyPool}
              aria-label={`更新时使用代理池 ${useProxyPool ? "关闭" : "开启"}`}
              disabled={saving || loading}
              onClick={() =>
                onSave([
                  {
                    key: UPDATE_USE_PROXY_POOL_KEY,
                    value: useProxyPool ? "0" : "1",
                  },
                ])
              }
              className={cn(
                "relative inline-flex h-7 w-12 shrink-0 cursor-pointer items-center rounded-full border transition-colors focus:outline-none focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50",
                useProxyPool
                  ? "border-accent-border bg-accent"
                  : "border-[var(--border)] bg-[var(--bg-2)]",
              )}
            >
              <span
                className={cn(
                  "h-5 w-5 rounded-full bg-[var(--bg-0)] shadow-[var(--shadow-1)] transition-transform",
                  useProxyPool ? "translate-x-5" : "translate-x-1",
                )}
              />
            </button>
            <div className="min-w-0">
              <p className="type-body-sm font-medium text-[var(--fg-0)]">
                更新时使用代理池
              </p>
              <p className="type-caption text-[var(--fg-2)]">
                国内服务器拉取 GitHub、uv 或 npm 资源失败时开启。
              </p>
            </div>
          </div>

          <label className="block rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-3">
            <span className="type-caption text-[var(--fg-2)]">更新代理</span>
            <select
              value={proxyName}
              disabled={saving || loading}
              onChange={(event) =>
                onSave([
                  {
                    key: UPDATE_PROXY_NAME_KEY,
                    value: event.target.value,
                  },
                ])
              }
              className="mt-2 h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 type-body-sm text-[var(--fg-0)] outline-none transition-colors focus:border-accent-border focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <option value="">自动选择第一个启用代理</option>
              {enabledProxies.map((proxy) => (
                <option key={proxy.name} value={proxy.name}>
                  {proxy.name}
                  {proxy.in_cooldown ? " · 冷却中" : ""}
                  {proxy.last_latency_ms != null
                    ? ` · ${Math.round(proxy.last_latency_ms)}ms`
                    : ""}
                </option>
              ))}
              {proxyName &&
                !enabledProxies.some((proxy) => proxy.name === proxyName) && (
                  <option value={proxyName}>{proxyName} · 当前配置</option>
                )}
            </select>
            <p className="mt-2 type-caption text-[var(--fg-2)]">
              留空会交给后端选择第一个启用代理；这里保存后立即影响下一次更新。
            </p>
          </label>
        </div>
      )}
    </div>
  );
}
