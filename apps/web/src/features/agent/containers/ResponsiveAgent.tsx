"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { AUTH_USER_QUERY_KEY } from "@/components/QueryProvider";
import { MobileTabBar } from "@/components/ui/shell/MobileTabBar";
import { MobileTopBar } from "@/components/ui/shell/MobileTopBar";
import { ErrorState, Spinner } from "@/components/ui/primitives";
import { useIsMobile } from "@/hooks/useMediaQuery";
import { getMe, type AuthUser } from "@/lib/apiClient";
import {
  getFirstVisibleNavRouteExcluding,
  normalizeNavVisibility,
} from "@/lib/navigation";
import { useUiStore } from "@/store/useUiStore";
import { useAgentStatusQuery } from "../api/queries";
import { AgentWorkspaceController } from "./AgentWorkspaceController";

export function ResponsiveAgent({ initialMobile }: { initialMobile: boolean }) {
  const detectedMobile = useIsMobile();
  const mobile = detectedMobile ?? initialMobile;
  const authoritative = useUiStore((state) => state.runtimeDefaultsAuthoritative);
  const meQuery = useQuery<AuthUser>({
    queryKey: AUTH_USER_QUERY_KEY,
    queryFn: getMe,
    retry: false,
    staleTime: 5 * 60_000,
  });
  if (meQuery.isLoading || (!authoritative && !meQuery.isError)) {
    return <AgentGateState mobile={mobile} loading />;
  }
  if (!meQuery.data || meQuery.isError) {
    return (
      <AgentGateState
        mobile={mobile}
        title="身份验证失败"
        detail="登录状态未能确认。"
        onRetry={() => void meQuery.refetch()}
      />
    );
  }
  return <AuthorizedAgentGate mobile={mobile} user={meQuery.data} />;
}

function AuthorizedAgentGate({
  mobile,
  user,
}: {
  mobile: boolean;
  user: AuthUser;
}) {
  const router = useRouter();
  const defaults = user.runtime_defaults;
  const visibility = normalizeNavVisibility(defaults?.nav_visibility);
  const visible =
    defaults?.agent_enabled === true && visibility.agent === true;
  const statusQuery = useAgentStatusQuery(visible);

  useEffect(() => {
    if (visible) return;
    router.replace(getFirstVisibleNavRouteExcluding("agent", visibility));
  }, [router, visibility, visible]);

  if (!visible) return <AgentGateState mobile={mobile} loading />;
  if (statusQuery.isLoading) return <AgentGateState mobile={mobile} loading />;
  if (statusQuery.isError || !statusQuery.data?.enabled) {
    return (
      <AgentGateState
        mobile={mobile}
        title="Agent 暂不可用"
        detail="Agent API 未能通过可用性检查。"
        onRetry={() => void statusQuery.refetch()}
      />
    );
  }
  return (
    <AgentWorkspaceController
      platform={mobile ? "mobile" : "desktop"}
      toolGatewayConfigured={statusQuery.data.tool_gateway_configured}
    />
  );
}

function AgentGateState({
  mobile,
  loading = false,
  title,
  detail,
  onRetry,
}: {
  mobile: boolean;
  loading?: boolean;
  title?: string;
  detail?: string;
  onRetry?: () => void;
}) {
  return (
    <div data-app-viewport className="flex h-[100dvh] min-h-0 flex-col bg-[var(--bg-0)]">
      {mobile ? (
        <MobileTopBar
          showWallet={false}
          left={<span className="type-card-title">Agent</span>}
        />
      ) : (
        <header className="flex h-[var(--appbar-h)] shrink-0 items-center gap-2 border-b border-[var(--border-subtle)] bg-[var(--bg-0)] px-5">
          <span className="type-nav font-semibold text-[var(--fg-0)]">Lumen</span>
          <span className="h-4 w-px bg-[var(--border-subtle)]" aria-hidden />
          <span className="type-nav text-accent">Agent</span>
        </header>
      )}
      <main className="flex min-h-0 flex-1 items-center justify-center px-4 pb-20">
        {loading ? (
          <div role="status" className="flex items-center gap-2 type-body-sm text-[var(--fg-2)]">
            <Spinner size={20} /> 加载中
          </div>
        ) : (
          <ErrorState
            icon={<AlertTriangle className="h-5 w-5" />}
            title={title ?? "Agent 暂不可用"}
            description={detail}
            onRetry={onRetry}
            retryLabel="重试"
            className="max-w-md"
          />
        )}
      </main>
      {mobile ? <MobileTabBar /> : null}
    </div>
  );
}
