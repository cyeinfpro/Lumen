"use client";

import { Input, Select, Switch } from "@/components/ui/primitives";
import type { Draft } from "./model";

export function DraftAgentCapabilityFields({
  draft,
  onUpdate,
}: {
  draft: Draft;
  onUpdate: (patch: Partial<Draft>) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 rounded-[var(--radius-panel)] border border-[var(--border-subtle)] bg-[var(--bg-2)] p-3 md:grid-cols-2 xl:grid-cols-3">
      <AgentField label="Responses API" hint="仅填写已由供应商或探活确认的能力">
        <CapabilitySelect
          value={draft.responses_supported ?? null}
          label="Responses API 能力"
          onChange={(responses_supported) => onUpdate({ responses_supported })}
        />
      </AgentField>
      <AgentField label="图片输入" hint="Agent 参考图只会选择明确支持的供应商">
        <CapabilitySelect
          value={draft.vision_supported ?? null}
          label="图片输入能力"
          onChange={(vision_supported) => onUpdate({ vision_supported })}
        />
      </AgentField>
      <AgentField label="Agent API">
        <Select
          value={draft.agent_api ?? "openai-responses"}
          onChange={(event) =>
            onUpdate({
              agent_api: event.target.value as NonNullable<Draft["agent_api"]>,
            })
          }
          aria-label="Agent Provider API"
        >
          <option value="openai-responses">OpenAI Responses</option>
          <option value="openai-completions">OpenAI Chat Completions</option>
          <option value="anthropic-messages">Anthropic Messages</option>
        </Select>
      </AgentField>
      <AgentField label="Agent SDK Base URL" hint="探活与实际 Agent 调用使用同一地址">
        <Input
          type="url"
          value={draft.agent_base_url ?? ""}
          placeholder={
            draft.agent_api === "anthropic-messages"
              ? "https://api.anthropic.com"
              : "https://api.openai.com/v1"
          }
          onChange={(event) => onUpdate({ agent_base_url: event.target.value })}
        />
      </AgentField>
      <AgentField label="上下文窗口" hint="模型可接收的总 token 上限">
        <BoundedInput
          value={draft.agent_context_window ?? 128_000}
          minimum={4096}
          maximum={2_000_000}
          fallback={128_000}
          onChange={(agent_context_window) => onUpdate({ agent_context_window })}
        />
      </AgentField>
      <AgentField label="单轮输出上限" hint="不得超过模型声明的输出 token 上限">
        <BoundedInput
          value={draft.agent_max_output_tokens ?? 16_384}
          minimum={1}
          maximum={128_000}
          fallback={16_384}
          onChange={(agent_max_output_tokens) =>
            onUpdate({ agent_max_output_tokens })
          }
        />
      </AgentField>
      <div className="flex flex-col">
        <span className="mb-1.5 type-caption font-medium text-[var(--fg-1)]">
          Reasoning
        </span>
        <div
          className={
            "flex min-h-10 flex-1 items-center justify-between gap-2 rounded-[var(--radius-control)] border px-3 " +
            (draft.agent_reasoning_supported !== false
              ? "border-success-border bg-success-soft"
              : "border-[var(--border-strong)] bg-[var(--bg-3)]")
          }
        >
          <span className="type-caption text-[var(--fg-1)]">
            {draft.agent_reasoning_supported !== false ? "支持" : "不支持"}
          </span>
          <Switch
            checked={draft.agent_reasoning_supported !== false}
            onCheckedChange={(agent_reasoning_supported) =>
              onUpdate({ agent_reasoning_supported })
            }
            aria-label="切换 Reasoning 能力"
          />
        </div>
      </div>
    </div>
  );
}

function CapabilitySelect({
  value,
  label,
  onChange,
}: {
  value: boolean | null;
  label: string;
  onChange: (value: boolean | null) => void;
}) {
  return (
    <Select
      value={value === null ? "unknown" : value ? "supported" : "unsupported"}
      onChange={(event) =>
        onChange(
          event.target.value === "unknown"
            ? null
            : event.target.value === "supported",
        )
      }
      aria-label={label}
    >
      <option value="unknown">未验证</option>
      <option value="supported">已验证支持</option>
      <option value="unsupported">已验证不支持</option>
    </Select>
  );
}

function BoundedInput({
  value,
  minimum,
  maximum,
  fallback,
  onChange,
}: {
  value: number;
  minimum: number;
  maximum: number;
  fallback: number;
  onChange: (value: number) => void;
}) {
  return (
    <Input
      type="number"
      min={minimum}
      max={maximum}
      value={value}
      onChange={(event) =>
        onChange(
          Math.max(
            minimum,
            Math.min(maximum, Number(event.target.value) || fallback),
          ),
        )
      }
      inputMode="numeric"
    />
  );
}

function AgentField({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex min-w-0 flex-col">
      <span className="mb-1.5 type-caption font-medium text-[var(--fg-1)]">
        {label}
      </span>
      {children}
      {hint ? (
        <span className="mt-1 type-caption leading-4 text-[var(--fg-2)]">
          {hint}
        </span>
      ) : null}
    </label>
  );
}
