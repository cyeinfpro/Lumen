import { Type, type Static } from "typebox";
import { Value } from "typebox/value";
import { isIP } from "node:net";

export const AGENT_TOOL_CREATE_IMAGE = "lumen_create_image";
export const RUNTIME_HEARTBEAT_EVENT = "run.heartbeat";

export const TERMINAL_EVENT_TYPES = new Set([
  "run.completed",
  "run.failed",
  "run.cancelled",
]);

const Identifier = Type.String({ minLength: 1, maxLength: 96 });
const LimitedText = Type.String({ maxLength: 65_536 });
const ProviderApi = Type.Union([
  Type.Literal("openai-responses"),
  Type.Literal("openai-completions"),
  Type.Literal("anthropic-messages"),
]);
const ReasoningEffort = Type.Union([
  Type.Literal("off"),
  Type.Literal("minimal"),
  Type.Literal("low"),
  Type.Literal("medium"),
  Type.Literal("high"),
  Type.Literal("xhigh"),
  Type.Literal("max"),
]);

export const RuntimeReferenceSchema = Type.Object(
  {
    reference_label: Type.String({ pattern: "^ref_(?:[1-9]|[1-5][0-9]|6[0-4])$" }),
    role: Type.String({ minLength: 1, maxLength: 32 }),
    display_label: Type.Union([Type.String({ maxLength: 80 }), Type.Null()]),
    mime_type: Type.Union([
      Type.Literal("image/png"),
      Type.Literal("image/jpeg"),
      Type.Literal("image/webp"),
    ]),
    data_base64: Type.String({ minLength: 4, maxLength: 700_000 }),
  },
  { additionalProperties: false },
);

export const RuntimeHistoryMessageSchema = Type.Object(
  {
    message_id: Type.Optional(Identifier),
    role: Type.Union([Type.Literal("user"), Type.Literal("assistant")]),
    text: Type.String({ minLength: 1, maxLength: 20_000 }),
  },
  { additionalProperties: false },
);

const RuntimeCompactionSchema = Type.Object(
  {
    summary: Type.String({ minLength: 1, maxLength: 48_000 }),
    first_kept_message_id: Identifier,
    next_message_id: Identifier,
    tokens_before: Type.Integer({ minimum: 1, maximum: 2_000_000 }),
  },
  { additionalProperties: false },
);

export const RuntimeRequestSchema = Type.Object(
  {
    version: Type.Literal(1),
    run_id: Identifier,
    agent_session_id: Identifier,
    user_id: Identifier,
    execution_epoch: Type.Integer({ minimum: 1 }),
    user_message_id: Type.Optional(Identifier),
    assistant_message_id: Identifier,
    trace_id: Type.String({ pattern: "^[a-f0-9]{32}$" }),
    event_features: Type.Optional(
      Type.Array(Type.Literal("heartbeat-v1"), {
        maxItems: 1,
        uniqueItems: true,
      }),
    ),
    provider: Type.Object(
      {
        provider_id: Type.String({ minLength: 1, maxLength: 64, pattern: "^[A-Za-z0-9._:-]+$" }),
        api: ProviderApi,
        base_url: Type.String({ minLength: 8, maxLength: 2048 }),
        api_key: Type.String({ minLength: 1, maxLength: 8192 }),
        headers: Type.Record(
          Type.String({ minLength: 1, maxLength: 128 }),
          Type.String({ maxLength: 8192 }),
          { maxProperties: 32 },
        ),
        proxy_url: Type.Union([Type.String({ minLength: 8, maxLength: 2048 }), Type.Null()]),
        resolved_ips: Type.Array(Type.String({ minLength: 2, maxLength: 64 }), {
          maxItems: 4,
          uniqueItems: true,
        }),
        model: Type.String({ minLength: 1, maxLength: 256 }),
        context_window: Type.Integer({ minimum: 4096, maximum: 2_000_000 }),
        max_output_tokens: Type.Integer({ minimum: 1, maximum: 128_000 }),
        reasoning_supported: Type.Boolean(),
        vision_supported: Type.Boolean(),
      },
      { additionalProperties: false },
    ),
    system_prompt: LimitedText,
    history: Type.Array(RuntimeHistoryMessageSchema, { maxItems: 2048 }),
    compaction: Type.Optional(
      Type.Union([RuntimeCompactionSchema, Type.Null()]),
    ),
    current_prompt: Type.String({ minLength: 1, maxLength: 40_000 }),
    references: Type.Array(RuntimeReferenceSchema, { maxItems: 64 }),
    allowed_tools: Type.Array(Type.Literal(AGENT_TOOL_CREATE_IMAGE), { maxItems: 1 }),
    image_defaults: Type.Object(
      {
        count: Type.Integer({ minimum: 1, maximum: 4 }),
        aspect_ratio: Type.String({ minLength: 3, maxLength: 5 }),
        quality: Type.Union([Type.Literal("1k"), Type.Literal("2k"), Type.Literal("4k")]),
        render_quality: Type.Union([
          Type.Literal("auto"),
          Type.Literal("low"),
          Type.Literal("medium"),
          Type.Literal("high"),
        ]),
        background: Type.Union([
          Type.Literal("auto"),
          Type.Literal("opaque"),
          Type.Literal("transparent"),
        ]),
        output_format: Type.Union([
          Type.Literal("png"),
          Type.Literal("jpeg"),
          Type.Literal("webp"),
        ]),
      },
      { additionalProperties: false },
    ),
    tool_gateway_url: Type.Union([Type.String({ minLength: 8, maxLength: 2048 }), Type.Null()]),
    tool_capability: Type.Union([Type.String({ minLength: 32, maxLength: 8192 }), Type.Null()]),
    reasoning_effort: Type.Union([ReasoningEffort, Type.Null()]),
    limits: Type.Object(
      {
        max_turns: Type.Integer({ minimum: 1, maximum: 12 }),
        max_tool_calls: Type.Integer({ minimum: 0, maximum: 12 }),
        max_image_tool_calls: Type.Integer({ minimum: 0, maximum: 8 }),
        max_images_per_run: Type.Integer({ minimum: 1, maximum: 16 }),
        max_output_tokens: Type.Integer({ minimum: 1, maximum: 32_000 }),
        run_timeout_seconds: Type.Integer({ minimum: 10, maximum: 1800 }),
        tool_timeout_seconds: Type.Integer({ minimum: 5, maximum: 300 }),
        max_output_chars: Type.Integer({ minimum: 1024, maximum: 1_000_000 }),
      },
      { additionalProperties: false },
    ),
  },
  { additionalProperties: false },
);

export type RuntimeRequest = Static<typeof RuntimeRequestSchema>;
export type RuntimeReference = Static<typeof RuntimeReferenceSchema>;
export type RuntimeHistoryMessage = Static<typeof RuntimeHistoryMessageSchema>;

export interface RuntimeUsage {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cache_write_1h_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
}

export interface RuntimeEvent {
  version: 1;
  type: string;
  seq: number;
  run_id: string;
  execution_epoch: number;
  [key: string]: unknown;
}

function validUrl(raw: string, allowedProtocols: ReadonlySet<string>): boolean {
  try {
    const url = new URL(raw);
    return (
      allowedProtocols.has(url.protocol) &&
      url.username === "" &&
      url.password === "" &&
      url.hash === ""
    );
  } catch {
    return false;
  }
}

function strictRequestChecks(request: RuntimeRequest): void {
  const historyIds = request.history
    .map((message) => message.message_id)
    .filter((messageId): messageId is string => messageId !== undefined);
  if (new Set(historyIds).size !== historyIds.length) {
    throw new Error("duplicate history message id");
  }
  if (
    request.compaction !== undefined &&
    request.compaction !== null &&
    (historyIds.length !== request.history.length ||
      !historyIds.includes(request.compaction.first_kept_message_id) ||
      (!historyIds.includes(request.compaction.next_message_id) &&
        request.compaction.next_message_id !== request.user_message_id))
  ) {
    throw new Error("compaction boundary is absent from history");
  }
  const labels = request.references.map((reference) => reference.reference_label);
  if (new Set(labels).size !== labels.length) throw new Error("duplicate reference label");
  if (request.references.length > 0 && !request.provider.vision_supported) {
    throw new Error("provider does not support reference images");
  }
  if (request.limits.max_output_tokens > request.provider.max_output_tokens) {
    throw new Error("requested output limit exceeds provider capability");
  }
  const toolsEnabled = request.allowed_tools.length === 1;
  if (toolsEnabled !== Boolean(request.tool_gateway_url && request.tool_capability)) {
    throw new Error("tool gateway and capability must match the tool allowlist");
  }
  if (!validUrl(request.provider.base_url, new Set(["http:", "https:"]))) {
    throw new Error("invalid provider base URL");
  }
  if (
    request.tool_gateway_url !== null &&
    !validUrl(request.tool_gateway_url, new Set(["http:", "https:"]))
  ) {
    throw new Error("invalid tool gateway URL");
  }
  if (request.provider.proxy_url !== null) {
    const protocols = new Set(["http:", "https:", "socks5:", "socks5h:"]);
    try {
      const proxy = new URL(request.provider.proxy_url);
      if (!protocols.has(proxy.protocol) || proxy.hash !== "") throw new Error();
    } catch {
      throw new Error("invalid provider proxy URL");
    }
  }
  if (request.provider.proxy_url !== null && request.provider.resolved_ips.length > 0) {
    throw new Error("proxy and pinned provider addresses are mutually exclusive");
  }
  if (request.provider.resolved_ips.some((address) => isIP(address) === 0)) {
    throw new Error("provider DNS pin contains an invalid address");
  }
  for (const name of Object.keys(request.provider.headers)) {
    if (/^(host|content-length|cookie|set-cookie|authorization)$/iu.test(name)) {
      throw new Error("provider envelope contains a reserved header");
    }
  }
  for (const reference of request.references) {
    if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/u.test(reference.data_base64)) {
      throw new Error("invalid reference preview encoding");
    }
    if (Buffer.byteLength(reference.data_base64, "base64") > 512 * 1024) {
      throw new Error("reference preview exceeds the byte limit");
    }
  }
}

export function parseRuntimeRequest(value: unknown): RuntimeRequest {
  if (!Value.Check(RuntimeRequestSchema, value)) {
    const [first] = Value.Errors(RuntimeRequestSchema, value);
    throw new Error(first ? `invalid request: ${first.message}` : "invalid request");
  }
  const request = value;
  strictRequestChecks(request);
  return request;
}

export function isTerminalEvent(event: RuntimeEvent): boolean {
  return TERMINAL_EVENT_TYPES.has(event.type);
}
