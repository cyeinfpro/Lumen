"use client";

import { Bot, RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { Button, ErrorState, Spinner } from "@/components/ui/primitives";
import type { Generation } from "@/lib/types";
import type {
  AgentAssistantMessage,
  AgentMessage,
  AgentRun,
} from "../model/contracts";
import { AgentTurn } from "./AgentTurn";

const SUGGESTIONS = [
  "整理一个极简产品海报方向",
  "为新品写一组视觉创意概念",
  "生成一张自然光商品主图",
] as const;

export function AgentConversation({
  messages,
  runsById,
  generationsById,
  platform,
  loading,
  error,
  scrollToMessageId,
  onRetry,
  onPickSuggestion,
  onPreviewGeneration,
  onUseReference,
  onContinue,
  hasMore,
  loadingMore,
  onLoadOlder,
}: {
  messages: AgentMessage[];
  runsById: Record<string, AgentRun>;
  generationsById: Record<string, Generation>;
  platform: "desktop" | "mobile";
  loading: boolean;
  error: string | null;
  scrollToMessageId: string | null;
  onRetry: () => void;
  onPickSuggestion: (text: string) => void;
  onPreviewGeneration: (generation: Generation) => void;
  onUseReference: (generation: Generation) => void;
  onContinue: (message: AgentAssistantMessage) => void;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadOlder: () => void;
}) {
  useEffect(() => {
    if (!scrollToMessageId) return;
    const frame = window.requestAnimationFrame(() => {
      document
        .getElementById(`agent-message-${scrollToMessageId}`)
        ?.scrollIntoView({ block: "center", behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages.length, scrollToMessageId]);

  if (loading && messages.length === 0) {
    return (
      <div
        role="status"
        className="flex min-h-80 items-center justify-center gap-2 type-body-sm text-[var(--fg-2)]"
      >
        <Spinner size={20} /> 加载中
      </div>
    );
  }
  if (error && messages.length === 0) {
    return (
      <div className="mx-auto flex min-h-80 max-w-lg items-center px-4">
        <ErrorState
          title="会话加载失败"
          description="历史消息未能载入，发送已暂停。"
          detail={error}
          onRetry={onRetry}
          retryLabel="重试"
        />
      </div>
    );
  }
  if (messages.length === 0) {
    return (
      <div
        data-testid="agent-empty-state"
        className="agent-empty-state mx-auto flex min-h-[55vh] max-w-[var(--content-composer)] flex-col items-center justify-center px-4 py-12 text-center"
      >
        <span className="agent-empty-state-mark flex h-12 w-12 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--bg-1)] text-accent">
          <Bot className="h-5 w-5" aria-hidden />
        </span>
        <h1 className="agent-empty-state-title mt-4 type-section-title">
          Agent
        </h1>
        <div
          data-testid="agent-empty-suggestions"
          className="agent-empty-state-suggestions mt-5 flex max-w-xl flex-wrap justify-center gap-2"
        >
          {SUGGESTIONS.map((suggestion) => (
            <Button
              key={suggestion}
              variant="secondary"
              size="sm"
              className="shrink-0"
              onClick={() => onPickSuggestion(suggestion)}
            >
              {suggestion}
            </Button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div
      role="log"
      aria-live="polite"
      aria-label="Agent 对话"
      className="px-3 pb-4 sm:px-5"
    >
      {hasMore ? (
        <div className="flex justify-center py-3">
          <Button
            variant="ghost"
            size="sm"
            loading={loadingMore}
            onClick={onLoadOlder}
          >
            加载更早消息
          </Button>
        </div>
      ) : null}
      {messages.map((message) => {
        const run =
          message.role === "assistant" && message.agentRunId
            ? runsById[message.agentRunId]
            : undefined;
        const generationIds =
          message.role === "assistant"
            ? Array.from(
                new Set([
                  ...message.generationIds,
                  ...(run?.tool_calls.flatMap((tool) => tool.generation_ids) ??
                    []),
                ]),
              )
            : [];
        const generations = generationIds
          .map((id) => generationsById[id])
          .filter((generation): generation is Generation =>
            Boolean(generation),
          );
        return (
          <AgentTurn
            key={message.id}
            message={message}
            run={run}
            generations={generations}
            platform={platform}
            onPreviewGeneration={onPreviewGeneration}
            onUseReference={onUseReference}
            onContinue={onContinue}
          />
        );
      })}
      {error ? (
        <div
          role="alert"
          className="mx-auto mt-3 flex max-w-[var(--content-text)] items-center gap-2 border border-danger-border bg-danger-soft px-3 py-2 type-caption text-[var(--danger-fg)]"
        >
          <span className="min-w-0 flex-1">{error}</span>
          <Button
            variant="ghost"
            size="sm"
            onClick={onRetry}
            leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          >
            重试
          </Button>
        </div>
      ) : null}
    </div>
  );
}
