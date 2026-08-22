import type { GenerationSummary } from "@/features/assets";
import type { AttachmentRole, Generation } from "@/lib/types";
import type { AgentRealtimeStatus } from "@/store/agent/useAgentStore";
import type {
  AgentAssistantMessage,
  AgentDraft,
  AgentDraftAttachment,
  AgentImageDefaults,
  AgentMessage,
  AgentRun,
  AgentSession,
  AgentSessionPatchInput,
} from "../model/contracts";
import type { AgentPromptOption } from "./AgentContextBar";

export interface AgentWorkspaceProps {
  sessions: AgentSession[];
  currentSession: AgentSession | null;
  messages: AgentMessage[];
  runsById: Record<string, AgentRun>;
  generationsById: Record<string, Generation>;
  draft: AgentDraft;
  sessionsLoading: boolean;
  sessionsHaveMore: boolean;
  sessionsLoadingMore: boolean;
  sessionSearch: string;
  messagesLoading: boolean;
  messagesHaveMore: boolean;
  messagesLoadingMore: boolean;
  messagesError: string | null;
  creating: boolean;
  submitting: boolean;
  stopping: boolean;
  busySessionId: string | null;
  activeRun: AgentRun | null;
  realtimeStatus: AgentRealtimeStatus;
  toolGatewayConfigured: boolean;
  prompts: AgentPromptOption[];
  sessionSaving: boolean;
  scrollToMessageId: string | null;
  assetItems: GenerationSummary[];
  assetsLoading: boolean;
  assetsHaveMore: boolean;
  onLoadMoreAssets: () => void;
  onLoadMoreSessions: () => void;
  onSessionSearchChange: (query: string) => void;
  onLoadOlderMessages: () => void;
  onCreateSession: () => void;
  onSelectSession: (sessionId: string) => void;
  onRenameSession: (sessionId: string, title: string) => void;
  onArchiveSession: (session: AgentSession) => void;
  onDeleteSession: (sessionId: string) => Promise<void> | void;
  onPatchSession: (patch: AgentSessionPatchInput) => void;
  onRetryMessages: () => void;
  onPickSuggestion: (text: string) => void;
  onTextChange: (text: string) => void;
  onDraftChange: (patch: Partial<AgentDraft>) => void;
  onDefaultsChange: (patch: Partial<AgentImageDefaults>) => void;
  onUpload: (file: File, signal: AbortSignal) => Promise<AgentDraftAttachment>;
  onAddAttachment: (attachment: AgentDraftAttachment) => boolean;
  onRemoveAttachment: (imageId: string) => void;
  onMoveAttachment: (imageId: string, direction: -1 | 1) => void;
  onRoleChange: (imageId: string, role: AttachmentRole) => void;
  onPreviewAttachment: (attachment: AgentDraftAttachment) => void;
  onPickAsset: (item: GenerationSummary) => void;
  onPreviewGeneration: (generation: Generation) => void;
  onUseReference: (generation: Generation) => void;
  onContinue: (message: AgentAssistantMessage) => void;
  onSubmit: () => void;
  onStop: () => void;
  composerError: string | null;
  composerAction: { href: string; label: string } | null;
  onComposerError: (message: string | null) => void;
}
