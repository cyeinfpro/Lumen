"use client";

import { useState } from "react";

import {
  userBillingQueryKeys,
  useUserQueryScope,
} from "@/components/QueryProvider";
import { RedemptionPanel } from "./RedemptionPanel";
import { OverviewSubpanel } from "./billing/OverviewSubpanel";
import { PricingSubpanel } from "./billing/PricingSubpanel";

type BillingSubTab = "overview" | "pricing" | "codes" | "wallets";

const SUB_TABS: { key: BillingSubTab; label: string }[] = [
  { key: "overview", label: "概览" },
  { key: "pricing", label: "定价" },
  { key: "codes", label: "兑换码" },
  { key: "wallets", label: "用户钱包" },
];

function BillingPanelContent({
  tab,
  onGoPricing,
  userBillingRootQueryKey,
}: {
  tab: BillingSubTab;
  onGoPricing: () => void;
  userBillingRootQueryKey: readonly unknown[];
}) {
  if (tab === "overview") {
    return <OverviewSubpanel onGoPricing={onGoPricing} />;
  }
  if (tab === "pricing") {
    return (
      <PricingSubpanel userBillingRootQueryKey={userBillingRootQueryKey} />
    );
  }
  if (tab === "codes") {
    return <RedemptionPanel section="codes" />;
  }
  return <RedemptionPanel section="wallets" />;
}

export function BillingPanel() {
  const [tab, setTab] = useState<BillingSubTab>("overview");
  const userScope = useUserQueryScope();
  const userBillingRootQueryKey = userBillingQueryKeys.all(userScope.userId);

  return (
    <div className="space-y-5">
      <div className="overflow-x-auto scrollbar-thin">
        <div className="inline-flex rounded-full border border-[var(--border)] bg-[var(--bg-2)] p-1">
          {SUB_TABS.map((item) => {
            const active = tab === item.key;
            return (
              <button
                key={item.key}
                type="button"
                onClick={(event) => {
                  setTab(item.key);
                  event.currentTarget.scrollIntoView({
                    behavior: "smooth",
                    block: "nearest",
                    inline: "center",
                  });
                }}
                className={[
                  "rounded-full px-3.5 py-1.5 text-sm transition-colors",
                  active
                    ? "bg-[var(--accent)] text-black"
                    : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
                ].join(" ")}
              >
                {item.label}
              </button>
            );
          })}
        </div>
      </div>

      <BillingPanelContent
        tab={tab}
        onGoPricing={() => setTab("pricing")}
        userBillingRootQueryKey={userBillingRootQueryKey}
      />
    </div>
  );
}
