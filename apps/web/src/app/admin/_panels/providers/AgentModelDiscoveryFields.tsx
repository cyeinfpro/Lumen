"use client";

import { RefreshCw } from "lucide-react";
import { Button, Select, Switch } from "@/components/ui/primitives";
import type { Draft } from "./model";
import {
  modelProfileSourceLabel,
  type ProviderModelDiscoveryState,
} from "./modelDiscovery";

export function AgentModelDiscoveryFields({
  draft,
  discovery,
  currentDefaultModel,
  canDiscover,
  onDiscover,
  onSelect,
  onSetDefault,
}: {
  draft: Draft;
  discovery: ProviderModelDiscoveryState | undefined;
  currentDefaultModel: string;
  canDiscover: boolean;
  onDiscover: () => void;
  onSelect: (modelId: string) => void;
  onSetDefault: (enabled: boolean) => void;
}) {
  const loading = discovery?.status === "loading";
  return (
    <div className="grid gap-3 border-y border-[var(--border-subtle)] py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="type-label text-[var(--fg-0)]">Agent 模型</p>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            URL 和 Key 填好后自动读取；选择模型会应用推荐能力参数。
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          loading={loading}
          disabled={!canDiscover}
          onClick={onDiscover}
        >
          读取模型
        </Button>
      </div>

      <DiscoveryResult
        draft={draft}
        discovery={discovery}
        currentDefaultModel={currentDefaultModel}
        onSelect={onSelect}
        onSetDefault={onSetDefault}
      />
    </div>
  );
}

function DiscoveryResult({
  draft,
  discovery,
  currentDefaultModel,
  onSelect,
  onSetDefault,
}: {
  draft: Draft;
  discovery: ProviderModelDiscoveryState | undefined;
  currentDefaultModel: string;
  onSelect: (modelId: string) => void;
  onSetDefault: (enabled: boolean) => void;
}) {
  if (discovery?.status === "error") {
    return (
      <p role="alert" className="type-caption text-[var(--danger-fg)]">
        {discovery.error || "模型读取失败"}
      </p>
    );
  }
  if (discovery?.status !== "ready" || discovery.models.length === 0) {
    return null;
  }
  const selected = discovery.models.find(
    (model) => model.id === discovery.selectedModelId,
  );
  const alreadyDefault = discovery.selectedModelId === currentDefaultModel;
  return (
    <>
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(220px,auto)]">
        <label className="grid min-w-0 gap-1.5 type-caption text-[var(--fg-2)]">
          可用模型 · {discovery.models.length}
          <Select
            value={discovery.selectedModelId ?? ""}
            onChange={(event) => onSelect(event.target.value)}
            aria-label={`${draft.name || "供应商"} Agent 模型`}
          >
            {discovery.models.map((model) => (
              <option key={model.id} value={model.id}>
                {model.id}
              </option>
            ))}
          </Select>
        </label>
        <DefaultModelControl
          currentDefaultModel={currentDefaultModel}
          alreadyDefault={alreadyDefault}
          checked={alreadyDefault || discovery.setAsDefault}
          onChange={onSetDefault}
        />
      </div>
      {selected ? <ModelProfileSummary model={selected} /> : null}
    </>
  );
}

function DefaultModelControl({
  currentDefaultModel,
  alreadyDefault,
  checked,
  onChange,
}: {
  currentDefaultModel: string;
  alreadyDefault: boolean;
  checked: boolean;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <div className="flex min-h-11 items-center justify-between gap-3 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3">
      <div className="min-w-0">
        <p className="type-caption font-medium text-[var(--fg-1)]">
          Agent 默认模型
        </p>
        <p className="truncate type-caption text-[var(--fg-2)]">
          {alreadyDefault ? "当前已是默认" : currentDefaultModel}
        </p>
      </div>
      <Switch
        checked={checked}
        disabled={alreadyDefault}
        onCheckedChange={onChange}
        aria-label="保存时设为 Agent 默认模型"
      />
    </div>
  );
}

function ModelProfileSummary({
  model,
}: {
  model: ProviderModelDiscoveryState["models"][number];
}) {
  return (
    <p className="type-caption text-[var(--fg-2)]">
      {modelProfileSourceLabel(model.profile.source)} ·{" "}
      {model.profile.agent_api} ·{" "}
      {model.profile.context_window.toLocaleString()} ctx ·{" "}
      {model.profile.max_output_tokens.toLocaleString()} 输出 · 图片
      {visionLabel(model.profile.vision_supported)} · Reasoning{" "}
      {model.profile.reasoning_supported ? "支持" : "不支持"}
    </p>
  );
}

function visionLabel(value: boolean | null): string {
  if (value === true) return "支持";
  if (value === false) return "不支持";
  return "待确认";
}
