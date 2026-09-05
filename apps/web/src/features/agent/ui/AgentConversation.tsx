"use client";

import {
  ArrowRight,
  FileSearch,
  Globe2,
  ImageIcon,
  RefreshCw,
} from "lucide-react";
import Image from "next/image";
import { useEffect } from "react";
import { Button, ErrorState, Skeleton } from "@/components/ui/primitives";
import type { Generation } from "@/lib/types";
import type {
  AgentAssistantMessage,
  AgentMessage,
  AgentRun,
} from "../model/contracts";
import type { AgentCapabilityAction } from "./AgentComposer";
import { AgentTurn } from "./AgentTurn";

interface CapabilityCard extends AgentCapabilityAction {
  icon: typeof ImageIcon;
  title: string;
  actionLabel: string;
  previewSrc: string;
  previewAlt: string;
}

const CAPABILITY_CARDS: CapabilityCard[] = [
  {
    kind: "visual",
    icon: ImageIcon,
    title: "多模态视觉企划",
    actionLabel: "选择图片",
    prompt:
      "分析我选择的产品图，提炼色彩风格和构图比例，为同一品牌规划一套春夏上新视觉；若生图工具可用，再生成首张方案图。",
    previewSrc: "/inspiration/editorial-fashion-portrait.webp",
    previewAlt: "时尚人物肖像的视觉企划预览",
  },
  {
    kind: "web",
    icon: Globe2,
    title: "商业与竞品调研",
    actionLabel: "开启联网",
    prompt:
      "联网搜索 2026 年极简美妆品牌视觉趋势，总结 3 个关键设计语言，附上来源，并输出可直接生图的 Prompt。",
    previewSrc: "/inspiration/rainy-cinematic-street.webp",
    previewAlt: "城市视觉趋势联网调研预览",
  },
  {
    kind: "file",
    icon: FileSearch,
    title: "设计素材批量分析",
    actionLabel: "选择文件",
    prompt:
      "读取我选择的设计素材和文本文件，归纳核心要求、冲突点与缺失信息，并整理出一组连贯的分镜设计方案。",
    previewSrc: "/inspiration/coastal-concept-architecture.webp",
    previewAlt: "概念设计文件批量分析预览",
  },
];

function AgentConversationSkeleton() {
  return (
    <div role="status" aria-label="正在载入 Agent 会话" className="px-4 py-6 sm:px-6">
      <span className="sr-only">正在载入 Agent 会话</span>
      <div className="mx-auto grid w-full max-w-[var(--content-media)] gap-8">
        <div className="ml-auto w-[min(76%,40rem)] rounded-[var(--radius-card)] border border-[var(--border-subtle)] p-4">
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="mt-2 h-4 w-1/2" />
        </div>
        <div className="flex gap-3">
          <Skeleton className="h-8 w-8 shrink-0 rounded-full" />
          <div className="min-w-0 flex-1">
            <Skeleton className="h-4 w-[min(100%,42rem)]" />
            <Skeleton className="mt-2 h-4 w-[min(82%,34rem)]" />
            <Skeleton className="mt-2 h-4 w-[min(64%,28rem)]" />
          </div>
        </div>
        <div className="ml-auto w-[min(58%,32rem)] rounded-[var(--radius-card)] border border-[var(--border-subtle)] p-4">
          <Skeleton className="h-4 w-full" />
        </div>
      </div>
    </div>
  );
}

export function AgentConversation({
  messages,
  runsById,
  generationsById,
  platform,
  loading,
  error,
  scrollToMessageId,
  onRetry,
  onStartCapability,
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
  onStartCapability: (action: AgentCapabilityAction) => void;
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
    return <AgentConversationSkeleton />;
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
        className="agent-empty-state mx-auto w-full max-w-[var(--content-composer)] px-3 py-3 sm:px-4 sm:py-8"
      >
        <h1 className="agent-empty-state-title type-card-title text-[var(--fg-0)] [@media(orientation:landscape)_and_(max-height:500px)]:hidden">
          Lumen Agent
        </h1>
        <div
          data-testid="agent-empty-suggestions"
          className="agent-empty-state-suggestions mt-3 grid w-full grid-cols-1 gap-2 sm:mt-6 sm:max-w-3xl sm:grid-cols-3 sm:gap-3 [@media(orientation:landscape)_and_(max-height:500px)]:mt-0 [@media(orientation:landscape)_and_(max-height:500px)]:gap-2"
        >
          {CAPABILITY_CARDS.map((card) => {
            const Icon = card.icon;
            return (
              <Button
                key={card.kind}
                variant="secondary"
                size="sm"
                onClick={() => onStartCapability(card)}
                aria-label={`${card.title}，${card.actionLabel}`}
                className="group h-auto min-h-[4.5rem] w-full justify-start gap-3 px-3 py-2 text-left sm:min-h-44 sm:flex-col sm:items-stretch sm:justify-between sm:p-4 [@media(orientation:landscape)_and_(max-height:500px)]:min-h-[3.25rem] [@media(orientation:landscape)_and_(max-height:500px)]:flex-row [@media(orientation:landscape)_and_(max-height:500px)]:items-center [@media(orientation:landscape)_and_(max-height:500px)]:gap-2 [@media(orientation:landscape)_and_(max-height:500px)]:p-1"
              >
                <span className="relative h-12 w-16 shrink-0 overflow-hidden rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)] sm:h-16 sm:w-full [@media(orientation:landscape)_and_(max-height:500px)]:h-10 [@media(orientation:landscape)_and_(max-height:500px)]:w-14">
                  <Image
                    src={card.previewSrc}
                    alt={card.previewAlt}
                    fill
                    sizes="(max-width: 767px) 64px, 220px"
                    className="object-cover"
                  />
                  <span className="absolute bottom-1 right-1 flex h-6 w-6 items-center justify-center rounded-full bg-[var(--media-control-bg)] text-[var(--media-control-fg)] backdrop-blur-sm">
                    <Icon className="h-3.5 w-3.5" aria-hidden />
                  </span>
                </span>
                <span className="min-w-0 flex-1 sm:mt-1">
                  <span className="block type-card-title text-[var(--fg-0)]">
                    {card.title}
                  </span>
                  <span className="mt-0.5 block type-caption text-accent sm:mt-3 [@media(orientation:landscape)_and_(max-height:500px)]:hidden">
                    {card.actionLabel}
                  </span>
                </span>
                <ArrowRight className="h-4 w-4 shrink-0 text-[var(--fg-2)] transition-transform duration-[var(--dur-fast)] group-hover:translate-x-0.5 motion-reduce:transform-none motion-reduce:transition-none sm:self-end [@media(orientation:landscape)_and_(max-height:500px)]:self-auto" aria-hidden />
              </Button>
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <div
      role="log"
      aria-live="off"
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
        <div className="mx-auto mt-3 flex max-w-[var(--content-text)] items-center gap-2 border border-danger-border bg-danger-soft px-3 py-2 type-caption text-[var(--danger-fg)]">
          <span role="alert" className="min-w-0 flex-1">{error}</span>
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
