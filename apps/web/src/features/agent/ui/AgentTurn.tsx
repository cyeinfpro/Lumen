"use client";

import { Bot, Copy, ImageIcon, Loader2 } from "lucide-react";
import { Markdown } from "@/components/ui/Markdown";
import { FinalImage } from "@/components/ui/chat/ConversationVisualAtoms";
import { Button } from "@/components/ui/primitives";
import type { Generation } from "@/lib/types";
import { aspectRatioToCss } from "@/lib/sizing";
import { cn } from "@/lib/utils";
import type {
  AgentAssistantMessage,
  AgentMessage,
  AgentOutputBlock,
  AgentRun,
  AgentToolCall as AgentToolCallContract,
  AgentUserMessage,
} from "../model/contracts";
import { neutralizeAgentPseudoProtocol } from "../model/agentTextSafety";
import { AgentRunStatus } from "./AgentRunStatus";
import { AgentToolCall } from "./AgentToolCall";

export function AgentTurn({
  message,
  run,
  generations,
  platform,
  onPreviewGeneration,
  onUseReference,
  onContinue,
}: {
  message: AgentMessage;
  run?: AgentRun;
  generations: Generation[];
  platform: "desktop" | "mobile";
  onPreviewGeneration: (generation: Generation) => void;
  onUseReference: (generation: Generation) => void;
  onContinue: (message: AgentAssistantMessage) => void;
}) {
  if (message.role === "user") return <AgentUserTurn message={message} />;
  return (
    <AgentAssistantTurn
      message={message}
      run={run}
      generations={generations}
      platform={platform}
      onPreviewGeneration={onPreviewGeneration}
      onUseReference={onUseReference}
      onContinue={onContinue}
    />
  );
}

function AgentUserTurn({ message }: { message: AgentUserMessage }) {
  return (
    <article
      id={`agent-message-${message.id}`}
      className="mx-auto w-full max-w-[var(--content-composer)] py-4"
      data-agent-message-id={message.id}
    >
      <div className="border-l border-[var(--border-strong)] pl-4">
        {message.attachments.length > 0 ? (
          <div className="mb-2 flex flex-wrap gap-2">
            {message.attachments.map((attachment, index) => (
              <figure key={`${attachment.image_id}:${index}`} className="w-16">
                <div className="h-16 w-16 overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-2)]">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={`/api/images/${encodeURIComponent(attachment.image_id)}/variants/thumb256`}
                    alt={`${attachment.label || roleLabel(attachment.role)} ${index + 1}`}
                    className="h-full w-full object-cover"
                    loading="lazy"
                  />
                </div>
                <figcaption className="mt-1 truncate type-caption text-[var(--fg-2)]">
                  {attachment.label || roleLabel(attachment.role)}
                </figcaption>
              </figure>
            ))}
          </div>
        ) : null}
        {message.text ? (
          <p className="whitespace-pre-wrap break-words type-body text-[var(--fg-0)] [overflow-wrap:anywhere]">
            {message.text}
          </p>
        ) : null}
      </div>
    </article>
  );
}

function AgentAssistantTurn({
  message,
  run,
  generations,
  platform,
  onPreviewGeneration,
  onUseReference,
  onContinue,
}: {
  message: AgentAssistantMessage;
  run?: AgentRun;
  generations: Generation[];
  platform: "desktop" | "mobile";
  onPreviewGeneration: (generation: Generation) => void;
  onUseReference: (generation: Generation) => void;
  onContinue: (message: AgentAssistantMessage) => void;
}) {
  const tools = toolCallsForTurn(message, run);
  const active = run?.status === "queued" || run?.status === "running";

  return (
    <article
      id={`agent-message-${message.id}`}
      data-agent-message-id={message.id}
      className="mx-auto w-full max-w-[var(--content-media)] py-4"
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] text-accent">
          <Bot className="h-4 w-4" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <AgentOrderedOutput
            message={message}
            tools={tools}
            active={active}
          />

          {generations.length > 0 ? (
            <div className="mt-3 grid gap-4 sm:grid-cols-2">
              {generations.map((generation) => (
                <AgentGeneration
                  key={generation.id}
                  generation={generation}
                  platform={platform}
                  onPreview={() => onPreviewGeneration(generation)}
                  onUseReference={() => onUseReference(generation)}
                />
              ))}
            </div>
          ) : null}

          {run ? (
            <AgentRunStatus
              run={run}
              onContinue={
                run.continuable === true
                  ? () => onContinue(message)
                  : undefined
              }
            />
          ) : null}
        </div>
      </div>
    </article>
  );
}

function toolForBlock(
  block: AgentOutputBlock,
  tools: AgentToolCallContract[],
): AgentToolCallContract | undefined {
  if (block.kind !== "tool") return undefined;
  if (block.ordinal !== undefined) {
    const byOrdinal = tools.find((tool) => tool.ordinal === block.ordinal);
    if (byOrdinal) return byOrdinal;
  }
  return tools.find((tool) => tool.id === block.tool_call_id);
}

function AgentOrderedOutput({
  message,
  tools,
  active,
}: {
  message: AgentAssistantMessage;
  tools: AgentToolCallContract[];
  active: boolean;
}) {
  const ordered = message.blocks.length > 0;
  if (!ordered && !message.text && tools.length === 0 && active) {
    return (
      <div role="status" className="flex min-h-10 items-center gap-2 type-body-sm text-[var(--fg-2)]">
        <Loader2 className="h-4 w-4 animate-spin text-accent" aria-hidden />
        Agent 运行中
      </div>
    );
  }
  const blocks = ordered
    ? message.blocks
    : [
        ...(message.text
          ? [{ kind: "text" as const, turn: 1, text: message.text }]
          : []),
        ...tools.map((tool) => ({
          kind: "tool" as const,
          turn: 1,
          tool_call_id: tool.id,
          ordinal: tool.ordinal,
        })),
      ];
  return (
    <div className="grid gap-3">
      {blocks.map((block, index) => {
        if (block.kind === "text") {
          return (
            <div key={`text:${block.turn}:${index}`} className="max-w-[var(--content-text)]">
              <Markdown className="type-body text-[var(--fg-0)]">
                {neutralizeAgentPseudoProtocol(block.text)}
              </Markdown>
              {active && index === blocks.length - 1 ? (
                <span className="ml-1 inline-block h-4 w-0.5 animate-pulse bg-accent align-text-bottom" aria-label="回复中" />
              ) : null}
            </div>
          );
        }
        const tool = toolForBlock(block, tools);
        return tool ? <AgentToolCall key={`tool:${tool.id}`} tool={tool} /> : null;
      })}
      {message.text ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={() =>
            void navigator.clipboard?.writeText(
              neutralizeAgentPseudoProtocol(message.text),
            )
          }
          aria-label="复制 Agent 回复"
          className="w-fit px-2 text-[var(--fg-2)]"
          leftIcon={<Copy className="h-3.5 w-3.5" aria-hidden />}
        >
          复制
        </Button>
      ) : null}
    </div>
  );
}

function toolCallsForTurn(
  message: AgentAssistantMessage,
  run: AgentRun | undefined,
): AgentToolCallContract[] {
  if (run?.tool_calls.length) {
    return [...run.tool_calls].sort((left, right) => left.ordinal - right.ordinal);
  }
  return message.toolCalls.map((tool, index) => ({
    id: tool.id ?? `${message.id}:tool:${index}`,
    agent_run_id: message.agentRunId ?? "",
    ordinal: index,
    name: tool.name ?? "lumen_create_image",
    mode: tool.mode ?? null,
    status: tool.status ?? "queued",
    generation_ids: tool.generation_ids ?? [],
    generation_count: tool.generation_count ?? tool.generation_ids?.length ?? 0,
    error_code: tool.error_code ?? null,
    started_at: null,
    finished_at: null,
    created_at: message.createdAt,
    updated_at: message.createdAt,
  }));
}

function AgentGeneration({
  generation,
  platform,
  onPreview,
  onUseReference,
}: {
  generation: Generation;
  platform: "desktop" | "mobile";
  onPreview: () => void;
  onUseReference: () => void;
}) {
  if (generation.status === "succeeded" && generation.image) {
    return (
      <FinalImage
        gen={generation}
        image={generation.image}
        platform={platform}
        inGrid
        onPreview={() => onPreview()}
        onCopy={() => void navigator.clipboard?.writeText(generation.prompt)}
        onEditImage={onUseReference}
      />
    );
  }
  const failed = generation.status === "failed" || generation.status === "canceled";
  return (
    <div
      role={failed ? "alert" : "status"}
      className={cn(
        "flex min-h-44 flex-col items-center justify-center gap-2 rounded-[var(--radius-card)] border p-4 text-center",
        failed
          ? "border-danger-border bg-danger-soft text-[var(--danger-fg)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]",
      )}
      style={{ aspectRatio: aspectRatioToCss(generation.aspect_ratio) }}
    >
      {failed ? (
        <>
          <ImageIcon className="h-5 w-5" aria-hidden />
          <span className="type-caption">{generation.status === "canceled" ? "已取消" : "图片生成失败"}</span>
        </>
      ) : (
        <>
          <Loader2 className="h-5 w-5 animate-spin text-accent" aria-hidden />
          <span className="type-caption">{generation.status === "queued" ? "图片排队中" : "图片生成中"}</span>
        </>
      )}
    </div>
  );
}

function roleLabel(role: string | undefined): string {
  const labels: Record<string, string> = {
    reference: "参考图",
    subject: "主体参考图",
    product: "产品参考图",
    style: "风格参考图",
    edit_target: "编辑目标图",
    background: "背景参考图",
  };
  return labels[role ?? ""] ?? "参考图";
}
