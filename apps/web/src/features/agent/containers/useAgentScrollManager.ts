"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { AgentMessage, AgentRun } from "../model/contracts";
import type { Generation } from "@/lib/types";
import { agentContentScrollAction, observeAgentContentResize, preferredAgentScrollBehavior } from "./agentScrollBehavior";

interface PrependAnchor {
  scrollHeight: number;
  scrollTop: number;
}

export function useAgentWorkspaceScroll({
  messages,
  runsById,
  generationsById,
  threshold,
  scrollToMessageId = null,
}: {
  messages: AgentMessage[];
  runsById: Record<string, AgentRun>;
  generationsById: Record<string, Generation>;
  threshold: number;
  scrollToMessageId?: string | null;
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
    localSubmissionId: latestMessage?.optimistic ? latestMessage.id : null,
    scrollTargetId: messages.some((message) => message.id === scrollToMessageId) ? scrollToMessageId : null,
    threshold,
  });
}

function useAgentScrollManager({
  contentVersion,
  hasContent,
  localSubmissionId,
  scrollTargetId,
  threshold,
}: {
  contentVersion: string;
  hasContent: boolean;
  localSubmissionId: string | null;
  scrollTargetId: string | null;
  threshold: number;
}) {
  const scrollRef = useRef<HTMLElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const previousSubmissionRef = useRef<string | null>(null);
  const previousTargetRef = useRef<string | null>(null);
  const pinnedRef = useRef(true);
  const prependAnchorRef = useRef<PrependAnchor | null>(null);
  const [newOutputBelow, setNewOutputBelow] = useState(false);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const update = () => {
      if (prependAnchorRef.current) return;
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
    if (scrollTargetId && scrollTargetId !== previousTargetRef.current) pinnedRef.current = false;
    previousTargetRef.current = scrollTargetId;
    const newLocalSubmission = localSubmissionId !== null && localSubmissionId !== previousSubmissionRef.current;
    previousSubmissionRef.current = localSubmissionId;
    const action = agentContentScrollAction({
      hasContent, hasPrependAnchor: Boolean(anchor), pinned: pinnedRef.current, newLocalSubmission,
    });
    if (action === "prepend" && anchor) {
      prependAnchorRef.current = null;
      root.scrollTop = anchor.scrollTop + (root.scrollHeight - anchor.scrollHeight);
      return;
    }
    if (action === "none") return;
    if (action === "latest") {
      root.scrollTo({
        top: root.scrollHeight,
        behavior: preferredAgentScrollBehavior(newLocalSubmission),
      });
      pinnedRef.current = true;
      setNewOutputBelow(false);
    } else {
      setNewOutputBelow(true);
    }
  }, [contentVersion, hasContent, localSubmissionId, scrollTargetId]);

  useEffect(() => {
    const root = scrollRef.current;
    const content = contentRef.current;
    if (!root || !content) return;
    return observeAgentContentResize({
      root,
      content,
      canFollow: () => hasContent && pinnedRef.current && !prependAnchorRef.current,
    });
  }, [hasContent]);

  const prepareForPrepend = useCallback((): (() => void) => {
    const root = scrollRef.current;
    if (!root) return () => undefined;
    const anchor = {
      scrollHeight: root.scrollHeight,
      scrollTop: root.scrollTop,
    };
    prependAnchorRef.current = anchor;
    pinnedRef.current = false;
    return () => {
      if (prependAnchorRef.current === anchor) prependAnchorRef.current = null;
    };
  }, []);

  const scrollToLatest = useCallback(() => {
    const root = scrollRef.current;
    if (!root) return;
    pinnedRef.current = true;
    setNewOutputBelow(false);
    root.scrollTo({ top: root.scrollHeight, behavior: preferredAgentScrollBehavior(true) });
  }, []);

  return {
    scrollRef,
    contentRef,
    newOutputBelow,
    prepareForPrepend,
    scrollToLatest,
  };
}
