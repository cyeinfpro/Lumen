import type {
  AspectRatio,
  AssistantMessage,
  AttachmentImage,
  Generation,
  GeneratedImage,
  ImageParams,
  Intent,
  MaskState,
  Message,
  Quality,
  RenderQualityChoice,
  SizeMode,
  UserMessage,
} from "../../lib/types";

export type ComposerMode = "image" | "chat";

export type ReasoningEffort =
  | "none"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh";

export interface ComposerState {
  text: string;
  attachments: AttachmentImage[];
  mode: ComposerMode;
  params: ImageParams;
  forceIntent?: "chat" | "image";
  reasoningEffort?: ReasoningEffort;
  fast: boolean;
  webSearch: boolean;
  fileSearch: boolean;
  codeInterpreter: boolean;
  imageGeneration: boolean;
  mask: MaskState | null;
}

export type PollInflightOptions = {
  signal?: AbortSignal;
  generationIds?: string[];
  completionIds?: string[];
  maxChecks?: number;
};

export interface ChatState {
  currentUserId: string | null;
  currentConvId: string | null;
  setCurrentUser: (id: string | null) => void;
  setCurrentConv: (id: string | null) => void;
  applyRuntimeDefaults: (defaults: {
    fast?: boolean;
    upload_max_source_bytes?: number;
  }) => void;

  messages: Message[];
  generations: Record<string, Generation>;
  imagesById: Record<string, GeneratedImage>;
  messagesCursor: string | null;
  messagesHasMore: boolean;
  messagesLoading: boolean;
  messagesError: string | null;

  composerError: string | null;
  setComposerError: (error: string | null) => void;

  composer: ComposerState;
  setText: (text: string) => void;
  setMode: (mode: ComposerMode) => void;
  setForceIntent: (value: ComposerState["forceIntent"]) => void;
  setAspectRatio: (aspect: AspectRatio) => void;
  setSizeMode: (mode: SizeMode) => void;
  setFixedSize: (size: string | undefined) => void;
  setQuality: (quality: Quality) => void;
  setRenderQuality: (quality: RenderQualityChoice) => void;
  setImageCount: (count: number) => void;
  setReasoningEffort: (value: ReasoningEffort | undefined) => void;
  setFast: (value: boolean) => void;
  setWebSearch: (value: boolean) => void;
  setFileSearch: (value: boolean) => void;
  setCodeInterpreter: (value: boolean) => void;
  setImageGeneration: (value: boolean) => void;
  addAttachment: (attachment: AttachmentImage) => void;
  removeAttachment: (id: string) => void;
  moveAttachment: (id: string, targetId: string) => void;
  setMask: (mask: MaskState) => void;
  clearMask: () => void;
  clearComposer: () => void;
  promoteImageToReference: (imageId: string) => void;

  uploadAttachment: (
    file: File,
    opts?: { signal?: AbortSignal },
  ) => Promise<AttachmentImage>;
  sendMessage: (opts?: {
    intentOverride?: Exclude<Intent, "auto">;
    restoreComposerOnFailure?: boolean;
  }) => Promise<void>;
  loadHistoricalMessages: (convId: string, loadMore?: boolean) => Promise<void>;
  retryAssistant: (assistantMsgId: string) => Promise<void>;
  retryGeneration: (generationId: string) => Promise<void>;
  regenerateAssistant: (
    assistantMsgId: string,
    newIntent: Exclude<Intent, "auto">,
  ) => Promise<void>;
  upscaleImage: (imageId: string) => Promise<void>;
  rerollImage: (imageId: string) => Promise<void>;
  submitInpaintTask: (input: {
    sourceImageId: string;
    sourceSrc: string;
    sourceWidth?: number;
    sourceHeight?: number;
    maskBlob: Blob;
    maskPreviewDataUrl: string;
    prompt: string;
  }) => Promise<void>;

  appendUserMessage: (message: UserMessage) => void;
  appendAssistantMessage: (message: AssistantMessage) => void;
  upsertGeneration: (generation: Generation) => void;
  attachImageToGeneration: (
    generationId: string,
    image: GeneratedImage,
  ) => void;
  applySSEEvent: (eventName: string, data: unknown) => void;

  pollInflightTasks: (opts?: PollInflightOptions) => Promise<void>;
  hydrateActiveTasks: (opts?: { signal?: AbortSignal }) => Promise<void>;
  refreshCompletionText: (
    completionId: string,
    opts?: { signal?: AbortSignal },
  ) => Promise<void>;
  reset: () => void;
}

export type ChatDataSlice = Pick<
  ChatState,
  | "currentUserId"
  | "currentConvId"
  | "messages"
  | "generations"
  | "imagesById"
  | "messagesCursor"
  | "messagesHasMore"
  | "messagesLoading"
  | "messagesError"
  | "composerError"
  | "composer"
>;

export type ChatStateGetter = () => ChatState;

export type ChatStateSetter = (
  partial:
    | ChatState
    | Partial<ChatState>
    | ((state: ChatState) => ChatState | Partial<ChatState>),
) => void;
