import type {
  AspectRatio,
  AttachmentRole,
  Generation,
} from "@/lib/types";
import type {
  BackendCompletion,
  BackendGeneration,
  BackendImageMeta,
} from "@/lib/api/tasks";

export const AGENT_MAX_REFERENCES = 16;
export const AGENT_MAX_FILES = 8;
export const AGENT_MAX_FILE_BYTES = 256 * 1024;
export const AGENT_MAX_TOTAL_FILE_BYTES = 1024 * 1024;
export const AGENT_NEW_DRAFT_KEY = "__new_agent_session__";

export type AgentRunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "cancelled";

export type AgentToolStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "timed_out";

export type AgentOutputFormat = "png" | "jpeg" | "webp";
export type AgentQuality = "1k" | "2k" | "4k";
export type AgentRenderQuality = "auto" | "low" | "medium" | "high";
export type AgentBackground = "auto" | "opaque" | "transparent";
export type AgentReasoningEffort =
  | "auto"
  | "none"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh"
  | "max";

export interface AgentImageDefaults {
  count: number;
  aspect_ratio: AspectRatio;
  quality: AgentQuality;
  render_quality: AgentRenderQuality;
  background: AgentBackground;
  output_format: AgentOutputFormat;
}

export const DEFAULT_AGENT_IMAGE_DEFAULTS: AgentImageDefaults = {
  count: 1,
  aspect_ratio: "1:1",
  quality: "2k",
  render_quality: "high",
  background: "auto",
  output_format: "webp",
};

export interface AgentReferenceInput {
  image_id: string;
  role: AttachmentRole;
  label?: string | null;
}

export interface AgentDraftAttachment {
  imageId: string;
  role: AttachmentRole;
  label: string | null;
  name: string;
  previewUrl: string;
  width?: number;
  height?: number;
  mime?: string;
}

export interface AgentDraftFile {
  name: string;
  mimeType: string;
  size: number;
  content: string;
}

export interface AgentDraft {
  text: string;
  attachments: AgentDraftAttachment[];
  files: AgentDraftFile[];
  allowImage: boolean;
  allowWebSearch: boolean;
  allowFileTools: boolean;
  imageDefaults: AgentImageDefaults;
  reasoningEffort?: AgentReasoningEffort;
}

export function createAgentDraft(
  overrides: Partial<AgentDraft> = {},
): AgentDraft {
  const { imageDefaults, ...rest } = overrides;
  return {
    text: "",
    attachments: [],
    files: [],
    allowImage: true,
    allowWebSearch: false,
    allowFileTools: true,
    ...rest,
    reasoningEffort: rest.reasoningEffort ?? "auto",
    imageDefaults: {
      ...DEFAULT_AGENT_IMAGE_DEFAULTS,
      ...imageDefaults,
    },
  };
}

export interface AgentReference {
  id: string;
  image_id: string;
  ordinal: number;
  reference_label: string;
  role: AttachmentRole | string;
  display_label: string | null;
}

export interface AgentToolCall {
  id: string;
  agent_run_id: string;
  ordinal: number;
  name: string;
  mode:
    | "text_to_image"
    | "image_to_image"
    | "web_search"
    | "file_list"
    | "file_read"
    | "file_search"
    | null;
  status: AgentToolStatus;
  generation_ids: string[];
  generation_count: number;
  error_code: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentRun {
  id: string;
  agent_session_id: string;
  user_message_id: string;
  assistant_message_id: string;
  status: AgentRunStatus;
  execution_epoch: number;
  last_event_seq: number;
  output_revision?: number;
  output_runtime_seq?: number;
  idempotency_key: string;
  model: string | null;
  reasoning_effort: string | null;
  memory_state?: "disabled" | "empty" | "ready" | "degraded" | null;
  continuable?: boolean;
  turn_count: number;
  tool_call_count: number;
  usage: Record<string, unknown>;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  cancel_requested_at: string | null;
  created_at: string;
  updated_at: string;
  references: AgentReference[];
  tool_calls: AgentToolCall[];
}

export interface AgentSession {
  id: string;
  conversation_id: string;
  title: string;
  pinned: boolean;
  archived: boolean;
  memory_disabled: boolean;
  active_scope_id: string | null;
  default_system: string | null;
  default_system_prompt_id: string | null;
  image_defaults: AgentImageDefaults;
  allow_image: boolean;
  allow_web_search: boolean;
  allow_file_tools: boolean;
  runtime_version: string;
  last_activity_at: string;
  created_at: string;
  updated_at: string;
  active_run: AgentRun | null;
}

export interface AgentSessionList {
  items: AgentSession[];
  next_cursor: string | null;
}

export interface AgentMessageAttachment {
  image_id: string;
  role?: string;
  label?: string | null;
  reference_label?: string;
  weight?: number;
}

export interface AgentMessageFile {
  name: string;
  mime_type: string;
  size: number;
}

export interface AgentMessageToolProjection {
  id?: string;
  name?: string;
  label?: string;
  mode?: AgentToolCall["mode"];
  status?: AgentToolStatus;
  generation_ids?: string[];
  generation_count?: number;
  error_code?: string | null;
  result_text?: string;
}

export interface AgentOutputTextBlock {
  kind: "text";
  turn: number;
  text: string;
}

export interface AgentOutputToolBlock {
  kind: "tool";
  turn: number;
  tool_call_id?: string;
  ordinal?: number;
  name?: string;
  status?: AgentToolStatus;
  generation_ids?: string[];
  result_text?: string;
}

export type AgentOutputBlock = AgentOutputTextBlock | AgentOutputToolBlock;

export interface AgentMessageContent {
  text?: string;
  source?: "agent" | string;
  agent_run_id?: string;
  blocks?: AgentOutputBlock[];
  output_revision?: number;
  output_runtime_seq?: number;
  attachments?: AgentMessageAttachment[];
  files?: AgentMessageFile[];
  tool_calls?: AgentMessageToolProjection[];
  generation_ids?: string[];
  images?: Array<Record<string, unknown>>;
  used_memory_summary?: Array<Record<string, unknown>>;
  reasoning_summary?: string;
  thinking_summary?: string;
}

export interface AgentBackendMessage {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system";
  content: AgentMessageContent;
  intent: string | null;
  status: string | null;
  parent_message_id: string | null;
  created_at: string;
}

export interface AgentUserMessage {
  id: string;
  role: "user";
  text: string;
  attachments: AgentMessageAttachment[];
  files: AgentMessageFile[];
  createdAt: string;
  optimistic?: boolean;
}

export interface AgentAssistantMessage {
  id: string;
  role: "assistant";
  text: string;
  status: AgentRunStatus | "pending";
  agentRunId: string | null;
  parentUserMessageId: string | null;
  generationIds: string[];
  toolCalls: AgentMessageToolProjection[];
  blocks: AgentOutputBlock[];
  outputRevision: number;
  outputRuntimeSeq: number;
  createdAt: string;
  partial: boolean;
  optimistic?: boolean;
}

export type AgentMessage = AgentUserMessage | AgentAssistantMessage;

export interface AgentMessageList {
  items: AgentBackendMessage[];
  runs: AgentRun[];
  next_cursor: string | null;
  generations: BackendGeneration[];
  completions: BackendCompletion[];
  images: BackendImageMeta[];
}

export interface AgentMessageCreateInput {
  idempotency_key: string;
  text: string;
  attachments: AgentReferenceInput[];
  files: Array<{
    name: string;
    mime_type: string;
    size: number;
    content: string;
  }>;
  image_defaults: AgentImageDefaults;
  allow_image: boolean;
  allow_web_search: boolean;
  allow_file_tools: boolean;
  reasoning_effort?: Exclude<AgentReasoningEffort, "auto"> | null;
}

export interface AgentSessionImage {
  image_id: string;
  reference_label: string;
  role: string;
  display_label: string | null;
  source: string;
  active: boolean;
}

export interface AgentSessionImageList {
  items: AgentSessionImage[];
  used: number;
  maximum: number;
}

export interface AgentMessageCreateResult {
  user_message: AgentBackendMessage;
  assistant_message: AgentBackendMessage;
  agent_run: AgentRun;
}

export interface AgentSessionCreateInput {
  title?: string;
  default_system?: string | null;
  default_system_prompt_id?: string | null;
  image_defaults?: AgentImageDefaults;
  allow_image?: boolean;
  allow_web_search?: boolean;
  allow_file_tools?: boolean;
}

export interface AgentSessionPatchInput {
  title?: string;
  pinned?: boolean;
  archived?: boolean;
  memory_disabled?: boolean;
  active_scope_id?: string | null;
  default_system?: string | null;
  default_system_prompt_id?: string | null;
  image_defaults?: AgentImageDefaults;
  allow_image?: boolean;
  allow_web_search?: boolean;
  allow_file_tools?: boolean;
}

export interface AgentStatus {
  enabled: boolean;
  tool_gateway_configured: boolean;
}

export const AGENT_EVENT_NAMES = [
  "agent.run.queued",
  "agent.run.started",
  "agent.output.delta",
  "agent.output.reset",
  "agent.tool.started",
  "agent.tool.updated",
  "agent.tool.succeeded",
  "agent.tool.failed",
  "agent.run.succeeded",
  "agent.run.partial",
  "agent.run.failed",
  "agent.run.cancelled",
] as const;

export type AgentEventName = (typeof AGENT_EVENT_NAMES)[number];

export interface AgentEventEnvelope {
  agent_session_id: string;
  agent_run_id: string;
  assistant_message_id: string;
  execution_epoch: number;
  event_seq: number;
  event_name: AgentEventName;
  text_delta?: string;
  text_operation?: "append" | "replace";
  replacement_text?: string;
  snapshot_required?: boolean;
  output_revision?: number;
  output_runtime_seq?: number;
  blocks?: AgentOutputBlock[];
  status?: AgentRunStatus;
  error_code?: string;
  tool_call_id?: string | null;
  generation_ids?: string[];
  event_id?: string;
}

export interface AgentGenerationProjection {
  byId: Record<string, Generation>;
  orderedIds: string[];
}

export const AGENT_TERMINAL_RUN_STATUSES = new Set<AgentRunStatus>([
  "succeeded",
  "partial",
  "failed",
  "cancelled",
]);

export function isAgentRunTerminal(status: AgentRunStatus): boolean {
  return AGENT_TERMINAL_RUN_STATUSES.has(status);
}
