"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { DesktopTopNav } from "@/components/ui/shell/DesktopTopNav";
import {
  DesktopPrivateSidebarDock,
  DesktopPrivateSidebarDrawer,
} from "@/components/ui/shell/PrivateSidebarShell";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { useUiStore } from "@/store/useUiStore";
import { AgentComposer } from "./AgentComposer";
import { AgentContextBar } from "./AgentContextBar";
import { AgentConversation } from "./AgentConversation";
import { AgentSidebar } from "./AgentSidebar";
import type { AgentWorkspaceProps } from "./AgentWorkspace.types";

export function DesktopAgent(props: AgentWorkspaceProps) {
  const sidebarOpen = useUiStore((state) => state.sidebarOpen);
  const toggleSidebar = useUiStore((state) => state.toggleSidebar);
  const wide = useMediaQuery("(min-width: 1440px)");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [composerHeight, setComposerHeight] = useState(120);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const workspaceRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLElement | null>(null);

  const toggle = useCallback(() => {
    if (wide === true) toggleSidebar();
    else setDrawerOpen((open) => !open);
  }, [toggleSidebar, wide]);

  useEffect(() => {
    const onToggle = () => toggle();
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "b") {
        event.preventDefault();
        toggle();
      }
    };
    window.addEventListener("lumen:sidebar-toggle", onToggle);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("lumen:sidebar-toggle", onToggle);
      window.removeEventListener("keydown", onKey);
    };
  }, [toggle]);

  useEffect(() => {
    if (!props.messages.length) return;
    const root = scrollRef.current;
    if (!root) return;
    const distance = root.scrollHeight - root.scrollTop - root.clientHeight;
    const localSubmission = props.messages.at(-1)?.optimistic === true;
    if (!localSubmission && distance > 140) return;
    root.scrollTo({ top: root.scrollHeight, behavior: "auto" });
  }, [props.messages, props.generationsById]);

  const sidebar = (
    <AgentSidebar
      sessions={props.sessions}
      currentSessionId={props.currentSession?.id ?? null}
      loading={props.sessionsLoading}
      creating={props.creating}
      hasMore={props.sessionsHaveMore}
      loadingMore={props.sessionsLoadingMore}
      query={props.sessionSearch}
      busySessionId={props.busySessionId}
      onCreate={props.onCreateSession}
      onSelect={props.onSelectSession}
      onRename={props.onRenameSession}
      onArchive={props.onArchiveSession}
      onDelete={props.onDeleteSession}
      onLoadMore={props.onLoadMoreSessions}
      onQueryChange={props.onSessionSearchChange}
    />
  );

  return (
    <div
      className="studio-shell relative flex h-[100dvh] min-h-0 flex-col bg-[var(--bg-0)]"
      data-sidebar-open={wide === true && sidebarOpen ? "true" : "false"}
      data-agent-workspace
    >
      <div ref={workspaceRef} className="flex min-h-0 flex-1 flex-col">
        <DesktopTopNav
          active="agent"
          onToggleSidebar={toggle}
          sidebarTriggerRef={triggerRef}
          sidebarExpanded={wide === true ? sidebarOpen : drawerOpen}
        />
        <div className="flex min-h-0 flex-1">
          <DesktopPrivateSidebarDock
            expanded={wide === true && sidebarOpen}
            onToggle={toggle}
            onCreate={props.onCreateSession}
            creating={props.creating}
            label="Agent 会话导航"
          >
            {sidebar}
          </DesktopPrivateSidebarDock>
          <section className="relative flex min-w-0 flex-1 flex-col">
            <AgentContextBar
              platform="desktop"
              session={props.currentSession}
              realtimeStatus={props.realtimeStatus}
              toolGatewayConfigured={props.toolGatewayConfigured}
              prompts={props.prompts}
              saving={props.sessionSaving}
              onPatch={props.onPatchSession}
            />
            <main
              ref={scrollRef}
              data-app-scroll
              data-testid="agent-conversation-scroll"
              className="lumen-studio-bg min-h-0 flex-1 overflow-x-hidden overflow-y-auto"
              style={{
                paddingBottom: `${composerHeight + 28}px`,
                scrollPaddingBottom: `${composerHeight + 28}px`,
              }}
            >
              <AgentConversation
                messages={props.messages}
                runsById={props.runsById}
                generationsById={props.generationsById}
                platform="desktop"
                loading={props.messagesLoading}
                error={props.messagesError}
                scrollToMessageId={props.scrollToMessageId}
                onRetry={props.onRetryMessages}
                onPickSuggestion={props.onPickSuggestion}
                onPreviewGeneration={props.onPreviewGeneration}
                onUseReference={props.onUseReference}
                onContinue={props.onContinue}
                hasMore={props.messagesHaveMore}
                loadingMore={props.messagesLoadingMore}
                onLoadOlder={props.onLoadOlderMessages}
              />
            </main>
            <AgentComposer
              platform="desktop"
              {...composerProps(props)}
              onMetricsChange={setComposerHeight}
            />
          </section>
        </div>
      </div>
      <DesktopPrivateSidebarDrawer
        open={drawerOpen && wide !== true}
        onClose={() => setDrawerOpen(false)}
        backgroundRef={workspaceRef}
        returnFocusRef={triggerRef}
        title="Agent 会话侧栏"
      >
        {sidebar}
      </DesktopPrivateSidebarDrawer>
    </div>
  );
}

function composerProps(props: AgentWorkspaceProps) {
  return {
    draft: props.draft,
    submitting: props.submitting || props.creating,
    runActive: Boolean(props.activeRun),
    stopping: props.stopping,
    error: props.composerError,
    errorAction: props.composerAction,
    assetItems: props.assetItems,
    assetsLoading: props.assetsLoading,
    assetsHaveMore: props.assetsHaveMore,
    onLoadMoreAssets: props.onLoadMoreAssets,
    onTextChange: props.onTextChange,
    onDraftChange: props.onDraftChange,
    onDefaultsChange: props.onDefaultsChange,
    onUpload: props.onUpload,
    onAddAttachment: props.onAddAttachment,
    onRemoveAttachment: props.onRemoveAttachment,
    onMoveAttachment: props.onMoveAttachment,
    onRoleChange: props.onRoleChange,
    onPreviewAttachment: props.onPreviewAttachment,
    onPickAsset: props.onPickAsset,
    onSubmit: props.onSubmit,
    onStop: props.onStop,
    onError: props.onComposerError,
  };
}

export { composerProps as agentComposerProps };
