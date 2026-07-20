"use client";

import { Plus, RefreshCw, Save } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import {
  RedemptionSecretControl,
  SwitchField,
} from "../BillingPanelParts";
import {
  BULK_RATE_FIELDS,
  VIDEO_PRICING_VARIANTS,
  VIDEO_RESOLUTIONS,
  videoDraftKey,
  videoRowEnabled,
  videoRowResolutionEnabled,
  videoRowResolutionUpdatedAt,
  videoRuleLabel,
  type ModelRuleRow,
  type VideoResolution,
  type VideoRuleRow,
} from "./pricingModel";
import type {
  BulkPricingFormState,
  GlobalSettingsFormState,
  ImagePricingFormState,
  ModelPricingFormState,
  VideoPricingFormState,
} from "./usePricingForms";

export function GlobalSettingsSection({
  form,
}: {
  form: GlobalSettingsFormState;
}) {
  const { values } = form;
  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="type-card-title">全局设置</p>
          <p className="type-body-sm text-[var(--fg-2)]">
            开关、低余额提示和兑换码 secret 集中在这里。
          </p>
        </div>
        <Button
          variant="primary"
          size="sm"
          onClick={form.save}
          loading={form.savePending}
          leftIcon={<Save className="h-3.5 w-3.5" />}
        >
          保存设置
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-5">
        <SwitchField
          label="计费开关"
          checked={values.enabled === "1"}
          onChange={(checked) =>
            form.setValue("enabled", checked ? "1" : "0")
          }
        />
        <SwitchField
          label="允许负余额"
          checked={values.allowNegative === "1"}
          onChange={(checked) =>
            form.setValue("allowNegative", checked ? "1" : "0")
          }
        />
        <SwitchField
          label="发送框预估"
          checked={values.showEstimate === "1"}
          onChange={(checked) =>
            form.setValue("showEstimate", checked ? "1" : "0")
          }
        />
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">
            低余额提示 (¥)
          </span>
          <input
            value={values.lowBalanceRmb}
            onChange={(event) =>
              form.setValue("lowBalanceRmb", event.target.value)
            }
            inputMode="decimal"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
        </label>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">USD→RMB</span>
          <input
            value={values.rate}
            onChange={(event) => form.setValue("rate", event.target.value)}
            inputMode="decimal"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
        </label>
      </div>
      <RedemptionSecretControl
        configured={values.secretConfigured}
        confirmed={form.secretConfirmed}
        loading={form.rotateSecretPending}
        onConfirmedChange={form.setSecretConfirmed}
        onRotate={form.rotateSecret}
      />
    </Card>
  );
}

export function BulkPricingSection({
  form,
}: {
  form: BulkPricingFormState;
}) {
  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="type-card-title">缓存感知模型定价</p>
          <p className="type-body-sm text-[var(--fg-2)]">
            一次写入输入、输出、缓存、推理和长上下文价格。
          </p>
        </div>
        <Button
          variant="primary"
          size="sm"
          onClick={form.save}
          loading={form.savePending}
          disabled={!form.model.trim()}
          leftIcon={<Save className="h-3.5 w-3.5" />}
        >
          批量保存
        </Button>
      </div>
      <div className="grid gap-3 md:grid-cols-[1fr_180px_140px]">
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">模型</span>
          <input
            value={form.model}
            onChange={(event) =>
              form.setIdentityField("model", event.target.value)
            }
            placeholder="claude-sonnet-4-6"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
        </label>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">Channel</span>
          <input
            value={form.channel}
            onChange={(event) =>
              form.setIdentityField("channel", event.target.value)
            }
            placeholder="可空"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
        </label>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">匹配优先级</span>
          <input
            value={form.priority}
            onChange={(event) =>
              form.setIdentityField("priority", event.target.value)
            }
            inputMode="numeric"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
        </label>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {BULK_RATE_FIELDS.map((field) => (
          <label key={field.key} className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">
              {field.label}
            </span>
            <input
              value={form.rates[field.key] ?? ""}
              onChange={(event) =>
                form.setRate(field.key, event.target.value)
              }
              inputMode="decimal"
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
            />
          </label>
        ))}
      </div>
    </Card>
  );
}

function ImagePricingTable({ form }: { form: ImagePricingFormState }) {
  return (
    <div className="data-stack-on-mobile md:overflow-x-auto">
      <table className="w-full text-sm md:min-w-[680px]">
        <thead className="text-left text-[var(--fg-2)]">
          <tr className="border-b border-[var(--border-subtle)]">
            <th className="px-3 py-2">档位</th>
            <th className="px-3 py-2">像素下界</th>
            <th className="px-3 py-2">单价 (¥/张)</th>
            <th className="px-3 py-2">状态</th>
          </tr>
        </thead>
        <tbody>
          {form.rows.map(({ tier, row, threshold }) => (
            <tr
              key={tier}
              className="border-b border-[var(--border-subtle)]"
            >
              <td data-label="档位" className="px-3 py-2 font-mono">
                {tier}
              </td>
              <td data-label="像素下界" className="px-3 py-2">
                <input
                  value={form.thresholds[tier] ?? String(threshold)}
                  onChange={(event) =>
                    form.setThreshold(tier, event.target.value)
                  }
                  inputMode="numeric"
                  className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
                />
              </td>
              <td data-label="单价 (¥/张)" className="px-3 py-2">
                <input
                  value={form.prices[tier] ?? row?.price.rmb ?? ""}
                  onChange={(event) =>
                    form.setPrice(tier, event.target.value)
                  }
                  inputMode="decimal"
                  placeholder="0.20"
                  className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
                />
              </td>
              <td data-label="状态" className="px-3 py-2">
                {row?.enabled === false ? "停用" : "启用"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ImagePricingSection({
  form,
}: {
  form: ImagePricingFormState;
}) {
  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="type-card-title">尺寸定价</p>
          <p className="type-body-sm text-[var(--fg-2)]">
            阈值和价格会在后端同一事务保存，避免前后端档位漂移。
          </p>
        </div>
        <Button
          variant="primary"
          size="sm"
          onClick={form.save}
          loading={form.savePending}
          leftIcon={<Save className="h-3.5 w-3.5" />}
        >
          保存尺寸定价
        </Button>
      </div>
      <ImagePricingTable form={form} />
      <div className="grid gap-3 border-t border-[var(--border-subtle)] pt-4 sm:grid-cols-[1fr_1fr_auto]">
        <input
          value={form.newTier}
          onChange={(event) => form.setNewTier(event.target.value)}
          placeholder="新增档位，如 8k"
          className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
        />
        <input
          value={form.newTierThreshold}
          onChange={(event) => form.setNewTierThreshold(event.target.value)}
          placeholder="像素下界，如 33177600"
          className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
        />
        <Button
          variant="outline"
          size="md"
          type="button"
          onClick={form.addTier}
          leftIcon={<Plus className="h-3.5 w-3.5" />}
        >
          添加档位
        </Button>
      </div>
    </Card>
  );
}

function VideoPriceInput({
  form,
  row,
  resolution,
  variant,
}: {
  form: VideoPricingFormState;
  row: VideoRuleRow;
  resolution: VideoResolution;
  variant: (typeof VIDEO_PRICING_VARIANTS)[number];
}) {
  const rule = row.rules[variant]?.[resolution];
  const fallback = row.rules[variant]?.base;
  const value =
    form.drafts[videoDraftKey(row.model, variant, resolution)] ??
    rule?.price.rmb ??
    fallback?.price.rmb ??
    "";
  return (
    <td data-label={videoRuleLabel(variant)} className="px-3 py-2">
      <input
        value={value}
        onChange={(event) =>
          form.setDraft(row.model, variant, resolution, event.target.value)
        }
        inputMode="decimal"
        className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
      />
    </td>
  );
}

function VideoPricingRow({
  form,
  row,
  resolution,
  index,
  modelEnabled,
}: {
  form: VideoPricingFormState;
  row: VideoRuleRow;
  resolution: VideoResolution;
  index: number;
  modelEnabled: boolean;
}) {
  const resolutionEnabled = videoRowResolutionEnabled(row, resolution);
  const updatedAt = videoRowResolutionUpdatedAt(row, resolution);
  const status = resolutionEnabled ? "启用" : modelEnabled ? "继承" : "停用";
  return (
    <tr className="border-b border-[var(--border-subtle)]">
      <td
        data-label="模型"
        className="px-3 py-2 font-mono text-xs [overflow-wrap:anywhere]"
      >
        {index === 0 ? row.model : ""}
      </td>
      <td data-label="分辨率" className="px-3 py-2 font-mono text-xs">
        {resolution}
      </td>
      {VIDEO_PRICING_VARIANTS.map((variant) => (
        <VideoPriceInput
          key={variant}
          form={form}
          row={row}
          resolution={resolution}
          variant={variant}
        />
      ))}
      <td data-label="状态" className="px-3 py-2">
        {status}
      </td>
      <td data-label="更新于" className="px-3 py-2 text-[var(--fg-2)]">
        {updatedAt ? new Date(updatedAt).toLocaleString() : "-"}
      </td>
      <td data-actions="true" className="px-3 py-2 text-right">
        {index === 0 ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => form.disable(row)}
            disabled={!modelEnabled}
          >
            停用
          </Button>
        ) : null}
      </td>
    </tr>
  );
}

function VideoModelRows({
  form,
  row,
}: {
  form: VideoPricingFormState;
  row: VideoRuleRow;
}) {
  const modelEnabled = videoRowEnabled(row);
  return VIDEO_RESOLUTIONS.map((resolution, index) => (
    <VideoPricingRow
      key={`${row.model}:${resolution}`}
      form={form}
      row={row}
      resolution={resolution}
      index={index}
      modelEnabled={modelEnabled}
    />
  ));
}

function VideoPricingTable({
  form,
  loading,
}: {
  form: VideoPricingFormState;
  loading: boolean;
}) {
  return (
    <div className="data-stack-on-mobile md:overflow-x-auto">
      <table className="w-full text-sm md:min-w-[1320px]">
        <thead className="text-left text-[var(--fg-2)]">
          <tr className="border-b border-[var(--border-subtle)]">
            <th className="px-3 py-2">模型</th>
            <th className="px-3 py-2">分辨率</th>
            <th className="px-3 py-2">T2V ¥/百万 token</th>
            <th className="px-3 py-2">I2V ¥/百万 token</th>
            <th className="px-3 py-2">参考图片 ¥/百万 token</th>
            <th className="px-3 py-2">参考视频 ¥/百万 token</th>
            <th className="px-3 py-2">Reference fallback</th>
            <th className="px-3 py-2">状态</th>
            <th className="px-3 py-2">更新于</th>
            <th className="px-3 py-2" />
          </tr>
        </thead>
        <tbody>
          {form.rows.flatMap((row) => (
            <VideoModelRows key={row.model} form={form} row={row} />
          ))}
          {!loading && form.rows.length === 0 && (
            <tr>
              <td
                className="px-3 py-8 text-center text-[var(--fg-2)]"
                colSpan={10}
              >
                暂无视频价格
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function NewVideoModelForm({ form }: { form: VideoPricingFormState }) {
  const fields: {
    key: keyof typeof form.newModel;
    placeholder: string;
    decimal?: boolean;
  }[] = [
    { key: "model", placeholder: "新增模型，如 seedance-2.0" },
    { key: "t2v", placeholder: "T2V 单价", decimal: true },
    { key: "i2v", placeholder: "I2V 单价", decimal: true },
    {
      key: "reference_image",
      placeholder: "参考图片单价",
      decimal: true,
    },
    {
      key: "reference_video",
      placeholder: "参考视频单价",
      decimal: true,
    },
    {
      key: "reference",
      placeholder: "Reference fallback",
      decimal: true,
    },
    { key: "note", placeholder: "备注" },
  ];
  return (
    <div className="grid gap-3 border-t border-[var(--border-subtle)] pt-4 md:grid-cols-[1fr_120px_120px_140px_140px_150px_1fr]">
      {fields.map((field) => (
        <input
          key={field.key}
          value={form.newModel[field.key]}
          onChange={(event) =>
            form.setNewModelField(field.key, event.target.value)
          }
          placeholder={field.placeholder}
          inputMode={field.decimal ? "decimal" : undefined}
          className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
        />
      ))}
    </div>
  );
}

export function VideoPricingSection({
  form,
  loading,
}: {
  form: VideoPricingFormState;
  loading: boolean;
}) {
  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="type-card-title">视频定价</p>
          <p className="type-body-sm text-[var(--fg-2)]">
            Seedance 按 token
            结算，这里配置每百万 token 的平台售价；预扣上界另由
            video.token_hold_estimates 按分辨率控制。
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <input
            value={form.officialMultiplier}
            onChange={(event) =>
              form.setOfficialMultiplier(event.target.value)
            }
            placeholder="官方价倍率"
            inputMode="decimal"
            className="h-9 w-28 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
          <Button
            variant="outline"
            size="sm"
            type="button"
            onClick={form.applyOfficialPricing}
          >
            按官方价填充
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={form.save}
            loading={form.savePending}
            disabled={!form.saveAvailable}
            leftIcon={<Save className="h-3.5 w-3.5" />}
          >
            保存视频定价
          </Button>
        </div>
      </div>
      <VideoPricingTable form={form} loading={loading} />
      <NewVideoModelForm form={form} />
    </Card>
  );
}

function ModelPricingRow({
  form,
  row,
}: {
  form: ModelPricingFormState;
  row: ModelRuleRow;
}) {
  const enabled = Boolean(row.input?.enabled || row.output?.enabled);
  return (
    <tr className="border-b border-[var(--border-subtle)]">
      <td
        data-label="模型"
        className="px-3 py-2 font-mono text-xs [overflow-wrap:anywhere]"
      >
        {row.model}
      </td>
      <td data-label="输入 ¥/1K" className="px-3 py-2">
        <input
          value={form.drafts[`${row.model}:in`] ?? row.input?.price.rmb ?? ""}
          disabled={!row.input}
          onChange={(event) =>
            form.setDraft(row.model, "in", event.target.value)
          }
          className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50 disabled:opacity-50"
        />
      </td>
      <td data-label="输出 ¥/1K" className="px-3 py-2">
        <input
          value={form.drafts[`${row.model}:out`] ?? row.output?.price.rmb ?? ""}
          disabled={!row.output}
          onChange={(event) =>
            form.setDraft(row.model, "out", event.target.value)
          }
          className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50 disabled:opacity-50"
        />
      </td>
      <td data-label="状态" className="px-3 py-2">
        {enabled ? "启用" : "停用"}
      </td>
      <td data-label="更新于" className="px-3 py-2 text-[var(--fg-2)]">
        {row.updated_at ? new Date(row.updated_at).toLocaleString() : "-"}
      </td>
      <td data-actions="true" className="px-3 py-2 text-right">
        <Button
          variant="outline"
          size="sm"
          onClick={() => form.disable(row)}
          disabled={!enabled}
        >
          停用
        </Button>
      </td>
    </tr>
  );
}

function ModelPricingTable({
  form,
  loading,
}: {
  form: ModelPricingFormState;
  loading: boolean;
}) {
  return (
    <div className="data-stack-on-mobile md:overflow-x-auto">
      <table className="w-full text-sm md:min-w-[840px]">
        <thead className="text-left text-[var(--fg-2)]">
          <tr className="border-b border-[var(--border-subtle)]">
            <th className="px-3 py-2">模型</th>
            <th className="px-3 py-2">输入 ¥/1K</th>
            <th className="px-3 py-2">输出 ¥/1K</th>
            <th className="px-3 py-2">状态</th>
            <th className="px-3 py-2">更新于</th>
            <th className="px-3 py-2" />
          </tr>
        </thead>
        <tbody>
          {form.rows.map((row) => (
            <ModelPricingRow key={row.model} form={form} row={row} />
          ))}
          {!loading && form.rows.length === 0 && (
            <tr>
              <td
                className="px-3 py-8 text-center text-[var(--fg-2)]"
                colSpan={6}
              >
                暂无模型价格
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function ModelPricingSection({
  form,
  rate,
  loading,
  onRateChange,
}: {
  form: ModelPricingFormState;
  rate: string;
  loading: boolean;
  onRateChange: (value: string) => void;
}) {
  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="type-card-title">对话模型定价</p>
          <p className="type-body-sm text-[var(--fg-2)]">
            可直接编辑当前模型，也可以粘贴 OpenAI 价目批量导入。
          </p>
        </div>
        <Button
          variant="primary"
          size="sm"
          onClick={form.save}
          loading={form.savePending}
          disabled={form.rows.length === 0}
          leftIcon={<Save className="h-3.5 w-3.5" />}
        >
          保存模型价格
        </Button>
      </div>
      <ModelPricingTable form={form} loading={loading} />
      <div className="grid gap-3 border-t border-[var(--border-subtle)] pt-4 md:grid-cols-[1fr_120px_auto]">
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">
            价目 YAML / JSON
          </span>
          <textarea
            value={form.priceFile}
            onChange={(event) => form.setPriceFile(event.target.value)}
            rows={5}
            placeholder="- model: gpt-5.5&#10;  input_usd_per_1m: 5.00&#10;  output_usd_per_1m: 15.00"
            className="w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
        </label>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">USD→RMB</span>
          <input
            value={rate}
            onChange={(event) => onRateChange(event.target.value)}
            inputMode="decimal"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
        </label>
        <Button
          variant="outline"
          size="md"
          onClick={form.importPricing}
          loading={form.importPending}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          导入
        </Button>
      </div>
    </Card>
  );
}
