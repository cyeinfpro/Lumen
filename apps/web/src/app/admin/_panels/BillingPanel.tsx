"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw, Save } from "lucide-react";

import {
  getAdminPricing,
  getSystemSettings,
  importOpenAiPricing,
  updateAdminPricing,
  updateSystemSettings,
} from "@/lib/apiClient";
import type { PricingRuleUpsertIn } from "@/lib/types";
import { Button, Card } from "@/components/ui/primitives";

const DEFAULT_IMAGE_THRESHOLDS: Record<string, number> = {
  "1k": 1_572_864,
  "2k": 3_686_400,
  "4k": 8_294_400,
};

export function BillingPanel() {
  const qc = useQueryClient();
  const pricingQ = useQuery({
    queryKey: ["admin", "pricing"],
    queryFn: getAdminPricing,
    retry: false,
  });
  const settingsQ = useQuery({
    queryKey: ["admin", "settings"],
    queryFn: getSystemSettings,
    retry: false,
  });
  const [imagePrices, setImagePrices] = useState<Record<string, string>>({});
  const [imageThresholds, setImageThresholds] = useState<Record<string, string>>({});
  const [customTiers, setCustomTiers] = useState<string[]>([]);
  const [newTier, setNewTier] = useState("");
  const [newTierThreshold, setNewTierThreshold] = useState("");
  const [priceFile, setPriceFile] = useState("");
  const [rateDraft, setRateDraft] = useState<string | null>(null);
  const [enabledDraft, setEnabledDraft] = useState<string | null>(null);
  const [lowBalanceDraft, setLowBalanceDraft] = useState<string | null>(null);
  const [thresholdsJsonDraft, setThresholdsJsonDraft] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const settingsByKey = useMemo(
    () => new Map(settingsQ.data?.items.map((item) => [item.key, item.value]) ?? []),
    [settingsQ.data?.items],
  );
  const savedEnabled = settingsByKey.get("billing.enabled");
  const enabled = enabledDraft ?? (savedEnabled === "0" || savedEnabled === "1" ? savedEnabled : "0");
  const lowBalance = lowBalanceDraft ?? settingsByKey.get("billing.low_balance_warn_micro") ?? "2000000";
  const rate = rateDraft ?? settingsByKey.get("billing.usd_to_rmb_rate") ?? "1.0";
  const thresholdsJson = thresholdsJsonDraft ?? settingsByKey.get("billing.image_size_thresholds") ?? "";

  const savedThresholds = useMemo(
    () => pricingQ.data?.image_size_thresholds ?? DEFAULT_IMAGE_THRESHOLDS,
    [pricingQ.data?.image_size_thresholds],
  );

  const imageRows = useMemo(() => {
    const tiers = new Set<string>(Object.keys(DEFAULT_IMAGE_THRESHOLDS));
    for (const key of Object.keys(savedThresholds)) tiers.add(key);
    for (const key of customTiers) tiers.add(key);
    for (const item of pricingQ.data?.items ?? []) {
      if (item.scope === "image_size" && item.unit === "per_image") tiers.add(item.key);
    }
    return Array.from(tiers)
      .sort((a, b) => (savedThresholds[a] ?? 0) - (savedThresholds[b] ?? 0) || a.localeCompare(b))
      .map((tier) => {
        const row = pricingQ.data?.items.find(
          (item) => item.scope === "image_size" && item.key === tier && item.unit === "per_image",
        );
        return { tier, row, threshold: savedThresholds[tier] ?? 0 };
      });
  }, [customTiers, pricingQ.data?.items, savedThresholds]);

  const saveImageMut = useMutation({
    mutationFn: async () => {
      const items: PricingRuleUpsertIn[] = imageRows.map(({ tier, row }) => ({
        scope: "image_size",
        key: tier,
        variant: "default",
        unit: "per_image",
        price_rmb: imagePrices[tier] ?? row?.price.rmb ?? "0",
        enabled: true,
        note: row?.note ?? "",
      }));
      const thresholds = Object.fromEntries(
        imageRows.map(({ tier, threshold }) => [
          tier,
          Number(imageThresholds[tier] ?? threshold) || 0,
        ]),
      );
      const nextThresholdsJson = JSON.stringify(thresholds);
      const out = await updateAdminPricing(items);
      await updateSystemSettings([
        { key: "billing.image_size_thresholds", value: nextThresholdsJson },
      ]);
      return { out, nextThresholdsJson };
    },
    onSuccess: async ({ nextThresholdsJson }) => {
      setStatus("尺寸定价已保存");
      setImagePrices({});
      setImageThresholds({});
      setCustomTiers([]);
      setThresholdsJsonDraft(nextThresholdsJson);
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["admin", "pricing"] }),
        qc.invalidateQueries({ queryKey: ["admin", "settings"] }),
        // User-facing /me/pricing has its own cache; bust it so PromptComposer
        // shows the new tier price without waiting for the staleTime to elapse.
        qc.invalidateQueries({ queryKey: ["me", "pricing"] }),
      ]);
    },
    onError: (err) => setStatus(err instanceof Error ? err.message : "保存失败"),
  });

  const importMut = useMutation({
    mutationFn: () => importOpenAiPricing(priceFile, Number(rate) || 1),
    onSuccess: async () => {
      setStatus("对话模型价格已导入");
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["admin", "pricing"] }),
        qc.invalidateQueries({ queryKey: ["me", "pricing"] }),
      ]);
    },
    onError: (err) => setStatus(err instanceof Error ? err.message : "导入失败"),
  });

  const settingsMut = useMutation({
    mutationFn: () =>
      updateSystemSettings([
        { key: "billing.enabled", value: enabled },
        { key: "billing.low_balance_warn_micro", value: lowBalance },
        { key: "billing.usd_to_rmb_rate", value: rate },
        { key: "billing.image_size_thresholds", value: thresholdsJson || JSON.stringify(savedThresholds) },
      ]),
    onSuccess: async () => {
      setStatus("全局开关已保存");
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["admin", "pricing"] }),
        qc.invalidateQueries({ queryKey: ["admin", "settings"] }),
        qc.invalidateQueries({ queryKey: ["me", "pricing"] }),
        qc.invalidateQueries({ queryKey: ["me", "wallet"] }),
      ]);
    },
    onError: (err) => setStatus(err instanceof Error ? err.message : "保存失败"),
  });

  const addTier = () => {
    const tier = newTier.trim().toLowerCase();
    if (!tier) {
      setStatus("请填写档位名称");
      return;
    }
    if (imageRows.some((row) => row.tier === tier)) {
      setStatus("档位已存在");
      return;
    }
    const threshold = Number(newTierThreshold) || 0;
    setCustomTiers((prev) => (prev.includes(tier) ? prev : [...prev, tier]));
    setImagePrices((prev) => ({ ...prev, [tier]: "0" }));
    setImageThresholds((prev) => ({ ...prev, [tier]: String(threshold) }));
    setNewTier("");
    setNewTierThreshold("");
  };

  const syncThresholdsJson = () => {
    const thresholds = Object.fromEntries(
      imageRows.map(({ tier, threshold }) => [
        tier,
        Number(imageThresholds[tier] ?? threshold) || 0,
      ]),
    );
    setThresholdsJsonDraft(JSON.stringify(thresholds));
  };

  return (
    <div className="space-y-5">
      {status && (
        <div className="rounded-[var(--radius-control)] border border-[var(--border)] bg-white/5 px-3 py-2 text-sm text-[var(--fg-1)]">
          {status}
        </div>
      )}

      <Card variant="subtle" padding="lg" className="space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="type-card-title">尺寸定价</p>
            <p className="type-body-sm text-[var(--fg-2)]">按张计费，单位 RMB。</p>
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={() => saveImageMut.mutate()}
            loading={saveImageMut.isPending}
            leftIcon={<Save className="h-3.5 w-3.5" />}
          >
            保存
          </Button>
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          {imageRows.map(({ tier, row }) => (
            <label key={tier} className="space-y-1.5">
              <span className="type-caption text-[var(--fg-2)]">{tier}</span>
              <input
                value={imagePrices[tier] ?? row?.price.rmb ?? ""}
                onChange={(e) =>
                  setImagePrices((prev) => ({ ...prev, [tier]: e.target.value }))
                }
                placeholder="0.20"
                className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
              />
              <input
                value={imageThresholds[tier] ?? String(savedThresholds[tier] ?? 0)}
                onChange={(e) =>
                  setImageThresholds((prev) => ({ ...prev, [tier]: e.target.value }))
                }
                placeholder="像素下界"
                className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] px-3 text-xs outline-none focus:border-[var(--accent)]/50"
              />
            </label>
          ))}
        </div>
        <div className="grid gap-3 border-t border-[var(--border-subtle)] pt-4 md:grid-cols-[1fr_1fr_auto]">
          <input
            value={newTier}
            onChange={(e) => setNewTier(e.target.value)}
            placeholder="新增档位，如 8k"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
          <input
            value={newTierThreshold}
            onChange={(e) => setNewTierThreshold(e.target.value)}
            placeholder="像素下界，如 33177600"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
          <Button
            variant="outline"
            size="md"
            type="button"
            onClick={addTier}
            leftIcon={<Plus className="h-3.5 w-3.5" />}
          >
            添加档位
          </Button>
        </div>
      </Card>

      <Card variant="subtle" padding="lg" className="space-y-4">
        <div>
          <p className="type-card-title">对话模型定价</p>
          <p className="type-body-sm text-[var(--fg-2)]">粘贴 OpenAI 价目 YAML/JSON，导入为 µRMB/1K tokens。</p>
        </div>
        <div className="grid gap-3 md:grid-cols-[1fr_120px_auto]">
          <textarea
            value={priceFile}
            onChange={(e) => setPriceFile(e.target.value)}
            rows={6}
            placeholder="- model: gpt-5.5&#10;  input_usd_per_1m: 5.00&#10;  output_usd_per_1m: 15.00"
            className="w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
          <input
            value={rate}
            onChange={(e) => setRateDraft(e.target.value)}
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
          <Button
            variant="outline"
            size="md"
            onClick={() => importMut.mutate()}
            loading={importMut.isPending}
            leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          >
            导入
          </Button>
        </div>
      </Card>

      <Card variant="subtle" padding="lg" className="space-y-4">
        <p className="type-card-title">全局开关</p>
        <div className="grid gap-3 md:grid-cols-3">
          <label className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">billing.enabled</span>
            <select
              value={enabled}
              onChange={(e) => setEnabledDraft(e.target.value)}
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            >
              <option value="0">关闭</option>
              <option value="1">开启</option>
            </select>
          </label>
          <label className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">USD→RMB</span>
            <input
              value={rate}
              onChange={(e) => setRateDraft(e.target.value)}
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            />
          </label>
          <label className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">低余额阈值 µRMB</span>
            <input
              value={lowBalance}
              onChange={(e) => setLowBalanceDraft(e.target.value)}
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            />
          </label>
        </div>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">image_size_thresholds JSON</span>
          <textarea
            value={thresholdsJson || JSON.stringify(savedThresholds)}
            onChange={(e) => setThresholdsJsonDraft(e.target.value)}
            onFocus={syncThresholdsJson}
            rows={3}
            className="w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-3 font-mono text-xs outline-none focus:border-[var(--accent)]/50"
          />
        </label>
        <Button
          variant="primary"
          size="sm"
          onClick={() => settingsMut.mutate()}
          loading={settingsMut.isPending}
          leftIcon={<Save className="h-3.5 w-3.5" />}
        >
          保存开关
        </Button>
      </Card>
    </div>
  );
}
