import { equal, match } from "node:assert/strict";
import { test } from "node:test";
import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";

type Estimate = { label: string | null; warning: boolean; amountRmb?: number };
function estimate(price: string | null, overrides: Record<string, unknown> = {}): Estimate {
  const queries = [
    { data: { id: "media-user", account_mode: "wallet" }, isLoading: false },
    { data: { items: [{ scope: "image_size", key: "1k", unit: "per_image", price: { rmb: price } }], ...overrides }, isLoading: false, isError: false },
  ];
  const { useComposerCostEstimate: calculate } = loadTsModule(new URL("../../../components/ui/composer/shared/useComposerCostEstimate.ts", import.meta.url), {
    react: { useMemo: (fn: () => Estimate) => fn() },
    "@tanstack/react-query": { useQuery: () => queries.shift() },
    "@/components/QueryProvider": {
      AUTH_USER_QUERY_KEY: ["auth"],
      useUserQueryScope: () => ({ enabled: true, userId: "media-user" }),
      userBillingQueryKeys: { pricing: (id: string) => ["pricing", id] },
    },
    "@/lib/apiClient": { getMe: () => {}, getPricing: () => {} },
    "@/lib/sizing": { qualityToFixedSize: () => ({ fixed_size: "1024x1024" }) },
  });
  return calculate({ mode: "image", quality: "1k", aspect: "1:1", count: 4 });
}

test("media image cost is an estimate, never a reserved or settled charge", () => {
  equal(estimate("0.125").label, "预计 ¥0.50");
  equal(estimate("0.125").amountRmb, 0.5);
  equal(estimate("0").label, "预计 ¥0.00");
  equal(estimate("1", { show_estimate_in_composer: false }).label, null);
  equal(estimate("1", { billing_enabled: false }).label, null);
});

test("media invalid pricing cannot masquerade as a free generation", () => {
  for (const price of [null, "", " ", "NaN", "Infinity", "-1"]) {
    equal(estimate(price).warning, true);
    match(estimate(price).label!, /暂不可用/);
  }
});
