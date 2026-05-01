"use client";

import dynamic from "next/dynamic";
import { memo } from "react";
import { UserBubble } from "./UserBubble";
import type { AssistantBubbleProps } from "./AssistantBubble";
import type { AssistantMessage, Generation, Intent, Message } from "@/lib/types";

interface MessageRowProps {
  msg: Message;
  generations: Record<string, Generation>;
  onEditImage: (imageId: string) => void;
  onRetry: (gen: Generation) => void;
  onRetryText: (assistantId: string) => void;
  onRegenerate: (
    assistantId: string,
    newIntent: Exclude<Intent, "auto">,
  ) => Promise<void>;
}

function generationIds(msg: AssistantMessage): string[] {
  if (msg.generation_ids?.length) return msg.generation_ids;
  return msg.generation_id ? [msg.generation_id] : [];
}

const AssistantBubble = dynamic<AssistantBubbleProps>(
  () => import("./AssistantBubble"),
  {
    ssr: false,
    loading: () => <AssistantBubbleFallback />,
  },
);

export const MessageRow = memo(function MessageRow({
  msg,
  generations,
  onEditImage,
  onRetry,
  onRetryText,
  onRegenerate,
}: MessageRowProps) {
  if (msg.role === "user") return <UserBubble msg={msg} />;

  const messageGenerations = generationIds(msg)
    .map((id) => generations[id])
    .filter((g): g is Generation => Boolean(g));

  return (
    <AssistantBubble
      msg={msg}
      generations={messageGenerations}
      onEditImage={onEditImage}
      onRetry={onRetry}
      onRetryText={() => onRetryText(msg.id)}
      onRegenerate={(newIntent) => onRegenerate(msg.id, newIntent)}
    />
  );
});

function AssistantBubbleFallback() {
  return (
    <div className="flex justify-start">
      <div className="h-14 w-full max-w-[96%] rounded-2xl rounded-bl-md border border-white/10 bg-white/[0.03]" />
    </div>
  );
}
