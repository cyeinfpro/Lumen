"use client";

import { useMemo, useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryKey,
} from "@tanstack/react-query";

import { AUTH_USER_QUERY_KEY } from "@/components/QueryProvider";
import { toast } from "@/components/ui/primitives";
import {
  getMe,
  getMyBillingSnapshot,
  getMyWallet,
  listMyRedemptions,
  listMyWalletTransactions,
  redeemCode,
  type AuthUser,
} from "@/lib/apiClient";
import { errorToText, mapError } from "@/lib/errors";
import { formatRmb } from "@/lib/money";

export const WALLET_TRANSACTION_FILTERS = [
  { key: "all", label: "全部" },
  { key: "topup_redeem", label: "兑换充值" },
  { key: "hold", label: "预扣" },
  { key: "settle", label: "结算" },
  { key: "release", label: "释放" },
  { key: "charge", label: "扣费" },
] as const;

export type WalletTransactionFilter =
  (typeof WALLET_TRANSACTION_FILTERS)[number]["key"];

type RedemptionNotice = {
  kind: "success" | "error";
  message: string;
};

export type WalletPageQueryScope = {
  userId: string | null | undefined;
  enabled: boolean;
};

export type WalletPageQueryKeys = {
  all: QueryKey;
  wallet: QueryKey;
  snapshot: QueryKey;
  transactions: (filter: WalletTransactionFilter) => QueryKey;
  redemptions: QueryKey;
};

function normalizeCode(value: string): string {
  const raw = value
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, "")
    .replace(/^LMN/, "");
  const chunks = raw.slice(0, 16).match(/.{1,4}/g) ?? [];
  return chunks.length ? `LMN-${chunks.join("-")}` : "LMN-";
}

function hasLowBalance(
  wallet: Awaited<ReturnType<typeof getMyWallet>> | undefined,
): boolean {
  if (!wallet?.balance || !wallet.low_balance_threshold) return false;
  return wallet.balance.micro < wallet.low_balance_threshold.micro;
}

function firstQueryError(...errors: unknown[]): string | null {
  const error = errors.find(Boolean);
  return error ? errorToText(error) : null;
}

export function useWalletPageModel({
  userScope,
  queryKeys,
}: {
  userScope: WalletPageQueryScope;
  queryKeys: WalletPageQueryKeys;
}) {
  const queryClient = useQueryClient();
  const [code, setCode] = useState("");
  const [notice, setNotice] = useState<RedemptionNotice | null>(null);
  const [transactionFilter, setTransactionFilter] =
    useState<WalletTransactionFilter>("all");

  const meQuery = useQuery<AuthUser>({
    queryKey: AUTH_USER_QUERY_KEY,
    queryFn: getMe,
    retry: false,
  });
  const identityReady =
    userScope.enabled && meQuery.data?.id === userScope.userId;
  const walletAccount =
    identityReady && meQuery.data?.account_mode === "wallet";
  const walletQuery = useQuery({
    queryKey: queryKeys.wallet,
    queryFn: getMyWallet,
    retry: false,
    enabled: identityReady,
  });
  const snapshotQuery = useQuery({
    queryKey: queryKeys.snapshot,
    queryFn: getMyBillingSnapshot,
    retry: false,
    enabled: walletAccount,
  });
  const transactionsQuery = useInfiniteQuery({
    queryKey: queryKeys.transactions(transactionFilter),
    queryFn: ({ pageParam }) =>
      listMyWalletTransactions({
        cursor: pageParam,
        kind: transactionFilter === "all" ? undefined : transactionFilter,
        limit: 30,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: false,
    enabled: walletAccount,
  });
  const redemptionsQuery = useInfiniteQuery({
    queryKey: queryKeys.redemptions,
    queryFn: ({ pageParam }) =>
      listMyRedemptions({ cursor: pageParam, limit: 20 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: false,
    enabled: walletAccount,
  });

  const transactions = useMemo(
    () =>
      transactionsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [transactionsQuery.data],
  );
  const redemptions = useMemo(
    () =>
      redemptionsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [redemptionsQuery.data],
  );

  const redeemMutation = useMutation({
    mutationFn: () => redeemCode(code),
    onSuccess: async (result) => {
      const amountText = `+¥${formatRmb(result.amount.rmb)}`;
      setCode("");
      setNotice({ kind: "success", message: amountText });
      toast.success("兑换成功", { description: amountText });
      await queryClient.invalidateQueries({
        queryKey: queryKeys.all,
      });
    },
    onError: (error) => {
      const normalized = mapError(error);
      const description = errorToText(error);
      setNotice({ kind: "error", message: description });
      toast.error(normalized.title, { description });
    },
  });

  function retryWallet() {
    if (!identityReady) {
      void meQuery.refetch();
      return;
    }
    void walletQuery.refetch();
  }

  async function copyRedemptionHistory() {
    const text = redemptions
      .map(
        (item) =>
          `${new Date(item.redeemed_at).toLocaleString()} ¥${formatRmb(item.amount.rmb)}`,
      )
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      toast.success("兑换记录已复制");
    } catch {
      toast.error("复制失败");
    }
  }

  return {
    accountMode: meQuery.data?.account_mode,
    wallet: walletQuery.data,
    lowBalance: hasLowBalance(walletQuery.data),
    walletState: {
      isLoading: !identityReady || walletQuery.isLoading,
      isRefreshing: meQuery.isFetching || walletQuery.isFetching,
      error: firstQueryError(meQuery.error, walletQuery.error),
      retry: retryWallet,
    },
    redemptionForm: {
      code,
      notice,
      isPending: redeemMutation.isPending,
      canSubmit:
        code.replace(/[^A-Z0-9]/g, "").length >= 19 &&
        !redeemMutation.isPending,
      setCode: (value: string) => setCode(normalizeCode(value)),
      submit: () => redeemMutation.mutate(),
    },
    snapshot: {
      data: snapshotQuery.data,
      isLoading: snapshotQuery.isLoading,
      isRefreshing: snapshotQuery.isRefetching,
      error: firstQueryError(snapshotQuery.error),
      refresh: () => void snapshotQuery.refetch(),
    },
    transactions: {
      items: transactions,
      filter: transactionFilter,
      setFilter: setTransactionFilter,
      isLoading: transactionsQuery.isLoading,
      isRefreshing: transactionsQuery.isRefetching,
      error: firstQueryError(transactionsQuery.error),
      refresh: () => void transactionsQuery.refetch(),
      hasNextPage: transactionsQuery.hasNextPage,
      isFetchingNextPage: transactionsQuery.isFetchingNextPage,
      loadMore: () => void transactionsQuery.fetchNextPage(),
    },
    redemptionHistory: {
      items: redemptions,
      isLoading: redemptionsQuery.isLoading,
      error: firstQueryError(redemptionsQuery.error),
      retry: () => void redemptionsQuery.refetch(),
      hasNextPage: redemptionsQuery.hasNextPage,
      isFetchingNextPage: redemptionsQuery.isFetchingNextPage,
      loadMore: () => void redemptionsQuery.fetchNextPage(),
      copy: copyRedemptionHistory,
    },
  };
}

export type WalletPageModel = ReturnType<typeof useWalletPageModel>;
