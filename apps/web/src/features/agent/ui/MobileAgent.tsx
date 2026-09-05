"use client";

import { ChevronDown, Plus } from "lucide-react";
import { useRef, useState } from "react";
import { LandscapeBanner } from "@/components/ui/shell/LandscapeBanner";
import { MobileConversationDrawer } from "@/components/ui/shell/MobileConversationDrawer";
import { MobileTabBar } from "@/components/ui/shell/MobileTabBar";
import { MobileTopBar } from "@/components/ui/shell/MobileTopBar";
import { MobileIconButton } from "@/components/ui/primitives/mobile/MobileIconButton";
import { Pressable } from "@/components/ui/primitives/mobile/Pressable";
import { TaskIsland } from "@/components/ui/tray/TaskIsland";
import { useKeyboardInset } from "@/hooks/useKeyboardInset";
import { useAgentWorkspaceScroll } from "../containers/useAgentScrollManager";
import {
  AgentComposer,
  type AgentComposerHandle,
} from "./AgentComposer";
import { AgentContextBar } from "./AgentContextBar";
import { AgentConversation } from "./AgentConversation";
import { AgentScrollToLatest } from "./AgentScrollToLatest";
import { AgentSidebar } from "./AgentSidebar";
import { agentComposerProps } from "./DesktopAgent";
import type { AgentWorkspaceProps } from "./AgentWorkspace.types";
import { currentAgentOperationLabel } from "./agentPresentation";

export function MobileAgent(props: AgentWorkspaceProps) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [composerHeight, setComposerHeight] = useState(120);
  const composerRef = useRef<AgentComposerHandle | null>(null);
  const { isKeyboardOpen } = useKeyboardInset();
  const {
    scrollRef,
    contentRef,
    newOutputBelow,
    prepareForPrepend,
    scrollToLatest,
  } = useAgentWorkspaceScroll({
    messages: props.messages,
    runsById: props.runsById,
    generationsById: props.generationsById,
    threshold: 96,
    scrollToMessageId: props.scrollToMessageId,
  });

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
      onNavigate={() => setDrawerOpen(false)}
      onLoadMore={props.onLoadMoreSessions}
      onQueryChange={props.onSessionSearchChange}
    />
  );

  return (
    <div
      data-app-viewport
      data-agent-workspace
      className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col overflow-hidden bg-[var(--bg-0)]"
      style={
        {
          "--mobile-composer-height": `${composerHeight}px`,
          "--agent-mobile-nav-offset": isKeyboardOpen
            ? "0px"
            : "var(--mobile-tabbar-height)",
          "--bottom-overlay-stack": `calc(var(--agent-mobile-nav-offset) + ${composerHeight}px + var(--space-5))`,
        } as React.CSSProperties
      }
    >
      <div className="shrink-0">
        <LandscapeBanner />
        <MobileTopBar
          showWallet={false}
          left={
            <Pressable
              size="default"
              minHit
              onPress={() => setDrawerOpen(true)}
              aria-label="打开 Agent 会话列表"
              className="min-w-0 flex-1 justify-start gap-1 px-2"
            >
              <span className="min-w-0 flex-1 truncate text-left type-body font-medium text-[var(--fg-0)]">
                会话列表
              </span>
              <ChevronDown
                className="h-4 w-4 shrink-0 text-[var(--fg-2)]"
                aria-hidden
              />
            </Pressable>
          }
          right={
            <MobileIconButton
              icon={<Plus className="h-5 w-5" />}
              label="新建 Agent 会话"
              onPress={props.onCreateSession}
              disabled={props.creating}
              minHit
            />
          }
          below={
            <div data-agent-mobile-context>
              <AgentContextBar
                platform="mobile"
                session={props.currentSession}
                realtimeStatus={props.realtimeStatus}
                operationLabel={currentAgentOperationLabel(props)}
                activeRun={props.activeRun}
                toolGatewayConfigured={props.toolGatewayConfigured}
                defaultModel={props.defaultModel}
                modelOptions={props.modelOptions}
                draft={props.draft}
                prompts={props.prompts}
                saving={props.sessionSaving}
                branching={props.branching}
                onBranch={props.onBranchSession}
                onPatch={props.onPatchSession}
                onDraftChange={props.onDraftChange}
                onDefaultsChange={props.onDefaultsChange}
                images={props.sessionImages}
                imagesLoading={props.sessionImagesLoading}
                removingImageId={props.sessionImageRemovingId}
                onEjectImage={props.onEjectSessionImage}
              />
            </div>
          }
        />
      </div>
      <main
        ref={scrollRef}
        data-app-scroll
        data-testid="agent-conversation-scroll"
        className="lumen-studio-bg min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain"
        style={{
          paddingBottom: `calc(var(--agent-mobile-nav-offset) + ${composerHeight}px + var(--space-5))`,
          scrollPaddingBottom: `calc(var(--agent-mobile-nav-offset) + ${composerHeight}px + var(--space-5))`,
        }}
      >
        <div ref={contentRef} data-agent-scroll-content>
        <AgentConversation
          messages={props.messages}
          runsById={props.runsById}
          generationsById={props.generationsById}
          platform="mobile"
          loading={props.messagesLoading}
          error={props.messagesError}
          scrollToMessageId={props.scrollToMessageId}
          onRetry={props.onRetryMessages}
          onStartCapability={(action) =>
            composerRef.current?.startCapability(action)
          }
          onPreviewGeneration={props.onPreviewGeneration}
          onUseReference={props.onUseReference}
          onContinue={props.onContinue}
          hasMore={props.messagesHaveMore}
          loadingMore={props.messagesLoadingMore}
          onLoadOlder={() => {
            const clearAnchor = prepareForPrepend();
            void Promise.resolve(props.onLoadOlderMessages()).finally(clearAnchor);
          }}
        />
        </div>
      </main>
      <AgentScrollToLatest
        visible={newOutputBelow}
        onClick={scrollToLatest}
        className="fixed right-3 z-[calc(var(--z-composer)+2)]"
        style={{
          bottom: `calc(var(--agent-mobile-nav-offset) + ${composerHeight}px + var(--space-3))`,
        }}
      />
      <div
        className="fixed left-1/2 z-[calc(var(--z-composer)+1)] -translate-x-1/2"
        style={{
          bottom: `calc(var(--agent-mobile-nav-offset) + ${composerHeight}px + var(--space-2))`,
        }}
      >
        <TaskIsland />
      </div>
      <AgentComposer
        ref={composerRef}
        platform="mobile"
        {...agentComposerProps(props)}
        onMetricsChange={setComposerHeight}
      />
      <MobileTabBar />
      <MobileConversationDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        ariaLabel="Agent 会话列表"
      >
        {sidebar}
      </MobileConversationDrawer>
    </div>
  );
}
