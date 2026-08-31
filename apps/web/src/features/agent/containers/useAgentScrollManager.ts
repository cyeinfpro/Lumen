"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { AgentMessage, AgentRun } from "../model/contracts";
import type { Generation } from "@/lib/types";

interface PrependAnchor {
  scrollHeight: number;
  scrollTop: number;
}

export function useAgentWorkspaceScroll({
  messages,
  runsById,
  generationsById,
  threshold,
}: {
  messages: AgentMessage[];
  runsById: Record<string, AgentRun>;
  generationsById: Record<string, Generation>;
  threshold: number;
}) {
  const latestMessage = messages.at(-1);
  const latestRun =
    latestMessage?.role === "assistant" && latestMessage.agentRunId
      ? runsById[latestMessage.agentRunId]
      : undefined;
  const outputSequence =
    latestMessage?.role === "assistant" ? latestMessage.outputRuntimeSeq : 0;
  return useAgentScrollManager({
    contentVersion: `${messages.length}:${latestMessage?.id ?? ""}:${outputSequence}:${latestRun?.updated_at ?? ""}:${Object.keys(generationsById).length}`,
    hasContent: messages.length > 0,
    localSubmission: latestMessage?.optimistic === true,
    threshold,
  });
}

function useAgentScrollManager({
  contentVersion,
  hasContent,
  localSubmission,
  threshold,
}: {
  contentVersion: string;
  hasContent: boolean;
  localSubmission: boolean;
  threshold: number;
}) {
  const scrollRef = useRef<HTMLElement | null>(null);
  const pinnedRef = useRef(true);
  const prependAnchorRef = useRef<PrependAnchor | null>(null);
  const [newOutputBelow, setNewOutputBelow] = useState(false);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const update = () => {
      const distance = root.scrollHeight - root.scrollTop - root.clientHeight;
      pinnedRef.current = distance <= threshold;
      if (pinnedRef.current) setNewOutputBelow(false);
    };
    update();
    root.addEventListener("scroll", update, { passive: true });
    return () => root.removeEventListener("scroll", update);
  }, [threshold]);

  useLayoutEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const anchor = prependAnchorRef.current;
    if (anchor) {
      prependAnchorRef.current = null;
      root.scrollTop = anchor.scrollTop + (root.scrollHeight - anchor.scrollHeight);
      return;
    }
    if (!hasContent) return;
    if (localSubmission || pinnedRef.current) {
      root.scrollTo({
        top: root.scrollHeight,
        behavior: localSubmission ? "smooth" : "auto",
      });
      pinnedRef.current = true;
      setNewOutputBelow(false);
    } else {
      setNewOutputBelow(true);
    }
  }, [contentVersion, hasContent, localSubmission]);

  const prepareForPrepend = useCallback((): (() => void) => {
    const root = scrollRef.current;
    if (!root) return () => undefined;
    const anchor = {
      scrollHeight: root.scrollHeight,
      scrollTop: root.scrollTop,
    };
    prependAnchorRef.current = anchor;
    return () => {
      if (prependAnchorRef.current === anchor) prependAnchorRef.current = null;
    };
  }, []);

  const scrollToLatest = useCallback(() => {
    const root = scrollRef.current;
    if (!root) return;
    pinnedRef.current = true;
    setNewOutputBelow(false);
    root.scrollTo({ top: root.scrollHeight, behavior: "smooth" });
  }, []);

  return {
    scrollRef,
    newOutputBelow,
    prepareForPrepend,
    scrollToLatest,
  };
}
