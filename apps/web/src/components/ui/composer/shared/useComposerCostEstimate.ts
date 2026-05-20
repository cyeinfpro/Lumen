"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { getMe, getPricing } from "@/lib/apiClient";
import { qualityToFixedSize } from "@/lib/sizing";
import type { AspectRatio, Quality } from "@/lib/types";

type ComposerMode = "chat" | "image";

const DEFAULT_IMAGE_SIZE_THRESHOLDS = {
  "1k": 1_572_864,
  "2k": 3_686_400,
  "4k": 8_294_400,
} as const;

export interface ComposerCostEstimate {
  label: string | null;
  warning: boolean;
  loading: boolean;
  amountRmb?: number;
  tier?: string;
}

function pixelsForQuality(quality: Quality, aspect: AspectRatio): number {
  const resolved = qualityToFixedSize(quality, aspect);
  const match = resolved.fixed_size?.match(/^(\d+)x(\d+)$/);
  if (!match) return DEFAULT_IMAGE_SIZE_THRESHOLDS["1k"];
  return Number(match[1]) * Number(match[2]);
}

function tierForPixels(
  pixels: number,
  thresholds: Record<string, number>,
): string {
  let tier = "1k";
  for (const [name, lower] of Object.entries(thresholds).sort(
    (a, b) => a[1] - b[1],
  )) {
    if (pixels >= lower) tier = name;
  }
  return tier;
}

export function useComposerCostEstimate(input: {
  mode: ComposerMode;
  quality: Quality;
  aspect: AspectRatio;
  count: number;
}): ComposerCostEstimate {
  const meQ = useQuery({
    queryKey: ["me"],
    queryFn: getMe,
    staleTime: 60_000,
    retry: false,
  });
  const isWalletAccount = meQ.data?.account_mode === "wallet";
  const pricingQ = useQuery({
    queryKey: ["me", "pricing"],
    queryFn: getPricing,
    enabled: isWalletAccount,
    staleTime: 5 * 60_000,
    retry: false,
  });

  return useMemo(() => {
    if (!isWalletAccount) {
      return { label: null, warning: false, loading: meQ.isLoading };
    }
    if (pricingQ.isLoading) {
      return { label: "费用读取中", warning: false, loading: true };
    }
    if (pricingQ.isError) {
      return { label: "费用暂不可用", warning: true, loading: false };
    }
    if (pricingQ.data?.billing_enabled === false) {
      return { label: null, warning: false, loading: false };
    }
    if (pricingQ.data?.show_estimate_in_composer === false) {
      return { label: null, warning: false, loading: false };
    }

    if (input.mode !== "image") {
      return { label: "按实际 token 计费", warning: false, loading: false };
    }

    const pixels = pixelsForQuality(input.quality, input.aspect);
    const thresholds =
      pricingQ.data?.image_size_thresholds ?? DEFAULT_IMAGE_SIZE_THRESHOLDS;
    const tier = tierForPixels(pixels, thresholds);
    const rule = pricingQ.data?.items.find(
      (item) =>
        item.scope === "image_size" &&
        item.key === tier &&
        item.unit === "per_image",
    );
    if (!rule) {
      return {
        label: "价格未配置",
        warning: true,
        loading: false,
        tier,
      };
    }
    const count = Math.max(1, Math.min(16, input.count || 1));
    const amountRmb = Number(rule.price.rmb ?? 0) * count;
    return {
      label: `预计扣 ¥${amountRmb.toFixed(2)}`,
      warning: false,
      loading: false,
      amountRmb,
      tier,
    };
  }, [
    input.aspect,
    input.count,
    input.mode,
    input.quality,
    isWalletAccount,
    meQ.isLoading,
    pricingQ.data,
    pricingQ.isError,
    pricingQ.isLoading,
  ]);
}
