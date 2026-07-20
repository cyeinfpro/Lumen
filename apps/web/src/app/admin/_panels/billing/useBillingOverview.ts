"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { toast } from "@/components/ui/primitives";
import {
  bootstrapAdminBilling,
  getAdminBillingOverview,
  listAdminOrphanHolds,
  releaseAdminOrphanHold,
  runAdminWalletAudit,
} from "@/lib/apiClient";
import type { AdminWalletAuditOut } from "@/lib/types";
import {
  buildBillingHealth,
  shouldShowBillingBootstrap,
} from "./overviewModel";

const OVERVIEW_QUERY_KEY = ["admin", "billing", "overview"] as const;
const ORPHAN_HOLDS_QUERY_KEY = [
  "admin",
  "billing",
  "orphan-holds",
] as const;

function errorDescription(error: unknown): string | undefined {
  return error instanceof Error ? error.message : undefined;
}

export function useBillingOverview() {
  const queryClient = useQueryClient();
  const [bootstrapRate, setBootstrapRate] = useState("1.0");
  const [auditResult, setAuditResult] = useState<AdminWalletAuditOut | null>(
    null,
  );
  const overviewQuery = useQuery({
    queryKey: OVERVIEW_QUERY_KEY,
    queryFn: getAdminBillingOverview,
    retry: false,
  });
  const orphanHoldsQuery = useQuery({
    queryKey: ORPHAN_HOLDS_QUERY_KEY,
    queryFn: () => listAdminOrphanHolds({ min_age_minutes: 60, limit: 20 }),
    retry: false,
  });
  const bootstrapMutation = useMutation({
    mutationFn: () =>
      bootstrapAdminBilling({
        enabled: true,
        usd_to_rmb_rate: Number(bootstrapRate) || 1,
      }),
    onSuccess: async () => {
      toast.success("计费初始化完成");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: ["admin", "settings"] }),
        queryClient.invalidateQueries({ queryKey: ["admin", "pricing"] }),
      ]);
    },
    onError: (error) =>
      toast.error("初始化失败", {
        description: errorDescription(error),
      }),
  });
  const auditMutation = useMutation({
    mutationFn: runAdminWalletAudit,
    onSuccess: (result) => {
      setAuditResult(result);
      if (result.ok) {
        toast.success("钱包对账通过", {
          description: `${result.transactions} 笔流水 / ${result.users} 个用户`,
        });
        return;
      }
      toast.error("钱包对账发现异常", {
        description: `${result.mismatch_count} 个不一致`,
      });
    },
    onError: (error) =>
      toast.error("对账失败", { description: errorDescription(error) }),
  });
  const releaseHoldMutation = useMutation({
    mutationFn: releaseAdminOrphanHold,
    onSuccess: async () => {
      toast.success("孤儿 hold 已释放");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ORPHAN_HOLDS_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: OVERVIEW_QUERY_KEY }),
      ]);
    },
    onError: (error) =>
      toast.error("释放失败", { description: errorDescription(error) }),
  });
  const overview = overviewQuery.data;

  return {
    auditResult,
    bootstrapRate,
    health: buildBillingHealth(overview),
    orphanHolds: orphanHoldsQuery.data ?? [],
    overview,
    overviewLoading: overviewQuery.isLoading,
    orphanHoldsLoading: orphanHoldsQuery.isLoading,
    bootstrapPending: bootstrapMutation.isPending,
    auditPending: auditMutation.isPending,
    releaseHoldPending: releaseHoldMutation.isPending,
    showBootstrap: shouldShowBillingBootstrap(overview),
    setBootstrapRate,
    refreshOverview: () => overviewQuery.refetch(),
    bootstrap: () => bootstrapMutation.mutate(),
    audit: () => auditMutation.mutate(),
    releaseHold: (txId: string) => releaseHoldMutation.mutate(txId),
  };
}

export type BillingOverviewState = ReturnType<typeof useBillingOverview>;
