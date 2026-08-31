import type {
  AgentAssistantMessage,
  AgentEventEnvelope,
  AgentEventName,
  AgentOutputBlock,
  AgentRun,
  AgentRunStatus,
} from "./contracts";
import { AGENT_EVENT_NAMES, isAgentRunTerminal } from "./contracts";

type RejectionReason = "wrong_run" | "stale_epoch" | "stale_sequence" | "terminal";
type OutputRevision = [number, number];

interface OutputProjection {
  text: string;
  blocks: AgentOutputBlock[];
  revision: number;
  runtimeSeq: number;
}

interface OutputRevisionState {
  current: OutputRevision;
  incoming: OutputRevision;
  revision: number;
  runtimeSeq: number;
}

export type AgentEventDecision =
  | { accepted: true; nextRun: AgentRun; nextMessage: AgentAssistantMessage }
  | { accepted: false; reason: RejectionReason };

export class AgentEventValidationError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("Invalid Agent realtime event");
    this.name = "AgentEventValidationError";
    this.code = code;
  }
}

const STATUS_BY_EVENT: Partial<Record<AgentEventName, AgentRunStatus>> = {
  "agent.run.queued": "queued",
  "agent.run.started": "running",
  "agent.run.succeeded": "succeeded",
  "agent.run.partial": "partial",
  "agent.run.failed": "failed",
  "agent.run.cancelled": "cancelled",
};

function eventStatus(eventName: AgentEventName, fallback: AgentRunStatus): AgentRunStatus {
  return STATUS_BY_EVENT[eventName] ?? fallback;
}

function eventIsTerminal(eventName: AgentEventName): boolean {
  return STATUS_BY_EVENT[eventName] !== undefined &&
    !new Set<AgentRunStatus>(["queued", "running"]).has(STATUS_BY_EVENT[eventName]);
}

function rejectionReason(
  run: AgentRun,
  message: AgentAssistantMessage,
  event: AgentEventEnvelope,
  nextStatus: AgentRunStatus,
): RejectionReason | null {
  const wrongScope =
    event.agent_session_id !== run.agent_session_id ||
    event.agent_run_id !== run.id ||
    event.assistant_message_id !== message.id;
  if (wrongScope) return "wrong_run";
  if (event.execution_epoch < run.execution_epoch) return "stale_epoch";
  const staleSequence =
    event.execution_epoch === run.execution_epoch &&
    event.event_seq <= run.last_event_seq;
  if (staleSequence) return "stale_sequence";
  const changesTerminal =
    isAgentRunTerminal(run.status) &&
    (!eventIsTerminal(event.event_name) || nextStatus !== run.status);
  return changesTerminal ? "terminal" : null;
}

function outputTuple(event: AgentEventEnvelope, run: AgentRun): OutputRevision {
  return [
    event.output_revision ?? run.output_revision ?? 0,
    event.output_runtime_seq ?? run.output_runtime_seq ?? 0,
  ];
}

function tupleIsOlder(left: OutputRevision, right: OutputRevision): boolean {
  return left[0] < right[0] || (left[0] === right[0] && left[1] < right[1]);
}

function tupleIsSame(left: OutputRevision, right: OutputRevision): boolean {
  return left[0] === right[0] && left[1] === right[1];
}

function appendTextBlock(
  blocks: AgentOutputBlock[],
  delta: string,
): AgentOutputBlock[] {
  const output = [...blocks];
  const last = output.at(-1);
  if (last?.kind !== "text") {
    output.push({ kind: "text", turn: 1, text: delta });
    return output;
  }
  output[output.length - 1] = { ...last, text: `${last.text}${delta}` };
  return output;
}

function unchangedOutput(message: AgentAssistantMessage): OutputProjection {
  return {
    text: message.text,
    blocks: message.blocks ?? [],
    revision: message.outputRevision ?? 0,
    runtimeSeq: message.outputRuntimeSeq ?? 0,
  };
}

function revisionState(
  run: AgentRun,
  message: AgentAssistantMessage,
  event: AgentEventEnvelope,
): OutputRevisionState {
  const current: OutputRevision = [
    run.output_revision ?? 0,
    run.output_runtime_seq ?? 0,
  ];
  const incoming = outputTuple(event, run);
  return {
    current,
    incoming,
    revision: Math.max(message.outputRevision ?? 0, incoming[0]),
    runtimeSeq: Math.max(message.outputRuntimeSeq ?? 0, incoming[1]),
  };
}

function resetProjection(
  event: AgentEventEnvelope,
  state: OutputRevisionState,
): OutputProjection {
  return {
    text: event.replacement_text ?? "",
    blocks: event.blocks ?? [],
    revision: state.revision,
    runtimeSeq: state.runtimeSeq,
  };
}

function deltaProjection(
  message: AgentAssistantMessage,
  event: AgentEventEnvelope,
  state: OutputRevisionState,
): OutputProjection {
  if (event.text_operation === "replace") {
    return {
      text: event.text_delta ?? "",
      blocks: event.blocks ?? [],
      revision: state.revision,
      runtimeSeq: state.runtimeSeq,
    };
  }
  const compatibilityReplay =
    event.output_revision !== undefined &&
    event.output_runtime_seq !== undefined &&
    tupleIsSame(state.incoming, state.current);
  const textDelta = event.text_delta ?? "";
  if (compatibilityReplay || textDelta.length === 0) {
    return {
      ...unchangedOutput(message),
      revision: state.revision,
      runtimeSeq: state.runtimeSeq,
    };
  }
  return {
    text: `${message.text}${textDelta}`,
    blocks: appendTextBlock(message.blocks ?? [], textDelta),
    revision: state.revision,
    runtimeSeq: state.runtimeSeq,
  };
}

function projectOutput(
  run: AgentRun,
  message: AgentAssistantMessage,
  event: AgentEventEnvelope,
): OutputProjection {
  const reset = event.event_name === "agent.output.reset";
  const delta = event.event_name === "agent.output.delta";
  if (!reset && !delta) return unchangedOutput(message);
  if (event.snapshot_required === true) return unchangedOutput(message);
  const state = revisionState(run, message, event);
  if (tupleIsOlder(state.incoming, state.current)) return unchangedOutput(message);
  return reset
    ? resetProjection(event, state)
    : deltaProjection(message, event, state);
}

export function applyAgentEvent(
  run: AgentRun,
  message: AgentAssistantMessage,
  event: AgentEventEnvelope,
): AgentEventDecision {
  const nextStatus = eventStatus(event.event_name, run.status);
  const rejected = rejectionReason(run, message, event, nextStatus);
  if (rejected) return { accepted: false, reason: rejected };

  const output = projectOutput(run, message, event);
  const generationIds = Array.from(
    new Set([...message.generationIds, ...(event.generation_ids ?? [])]),
  );
  return {
    accepted: true,
    nextRun: {
      ...run,
      status: nextStatus,
      execution_epoch: event.execution_epoch,
      last_event_seq: event.event_seq,
      output_revision: output.revision,
      output_runtime_seq: output.runtimeSeq,
      updated_at: new Date().toISOString(),
    },
    nextMessage: {
      ...message,
      text: output.text,
      blocks: output.blocks,
      outputRevision: output.revision,
      outputRuntimeSeq: output.runtimeSeq,
      status: nextStatus,
      generationIds,
      partial: message.partial || nextStatus === "partial",
    },
  };
}

function boundedString(value: unknown, maximum: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maximum;
}

function nonnegativeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0;
}

function positiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 1;
}

function validBlock(value: unknown): value is AgentOutputBlock {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as Record<string, unknown>;
  if (!positiveInteger(item.turn)) return false;
  if (item.kind === "text") {
    return typeof item.text === "string" && item.text.length <= 20_000;
  }
  const validToolId =
    item.tool_call_id === undefined || boundedString(item.tool_call_id, 128);
  const validOrdinal = item.ordinal === undefined || nonnegativeInteger(item.ordinal);
  const validResult =
    item.result_text === undefined ||
    (typeof item.result_text === "string" && item.result_text.length <= 20_000);
  return item.kind === "tool" && validToolId && validOrdinal && validResult;
}

function validBlocks(value: unknown): value is AgentOutputBlock[] {
  return Array.isArray(value) && value.length <= 32 && value.every(validBlock);
}

function assertEventScope(eventName: string, item: Record<string, unknown>): void {
  const supported = AGENT_EVENT_NAMES.includes(eventName as AgentEventName);
  const validIds =
    boundedString(item.agent_session_id, 64) &&
    boundedString(item.agent_run_id, 64) &&
    boundedString(item.assistant_message_id, 64);
  const validOrdering =
    nonnegativeInteger(item.execution_epoch) && positiveInteger(item.event_seq);
  if (!supported || item.event_name !== eventName || !validIds || !validOrdering) {
    throw new AgentEventValidationError("agent_event_scope_invalid");
  }
}

function assertOutputPayload(eventName: string, item: Record<string, unknown>): void {
  if (
    item.text_operation !== undefined &&
    item.text_operation !== "append" &&
    item.text_operation !== "replace"
  ) {
    throw new AgentEventValidationError("agent_event_text_operation_invalid");
  }
  if (
    item.snapshot_required !== undefined &&
    typeof item.snapshot_required !== "boolean"
  ) {
    throw new AgentEventValidationError("agent_event_snapshot_marker_invalid");
  }
  if (eventName === "agent.output.delta") {
    const validDelta =
      typeof item.text_delta === "string" && item.text_delta.length <= 20_000;
    if (!validDelta) throw new AgentEventValidationError("agent_event_delta_invalid");
  }
  if (eventName === "agent.output.reset" && item.replacement_text !== undefined) {
    const validReplacement =
      typeof item.replacement_text === "string" &&
      item.replacement_text.length <= 1_000_000;
    if (!validReplacement) {
      throw new AgentEventValidationError("agent_event_reset_invalid");
    }
  }
}

function assertRevisionPayload(item: Record<string, unknown>): void {
  for (const key of ["output_revision", "output_runtime_seq"] as const) {
    if (item[key] !== undefined && !nonnegativeInteger(item[key])) {
      throw new AgentEventValidationError("agent_event_revision_invalid");
    }
  }
  if (item.blocks !== undefined && !validBlocks(item.blocks)) {
    throw new AgentEventValidationError("agent_event_blocks_invalid");
  }
}

function assertTerminalPayload(item: Record<string, unknown>): void {
  if (
    item.status !== undefined &&
    !new Set(["queued", "running", "succeeded", "partial", "failed", "cancelled"])
      .has(String(item.status))
  ) {
    throw new AgentEventValidationError("agent_event_status_invalid");
  }
  if (
    item.error_code !== undefined &&
    (typeof item.error_code !== "string" || item.error_code.length > 64)
  ) {
    throw new AgentEventValidationError("agent_event_error_code_invalid");
  }
}

function assertGenerationIds(value: unknown): void {
  if (value === undefined) return;
  const valid =
    Array.isArray(value) &&
    value.length <= 4 &&
    value.every((id) => boundedString(id, 96));
  if (!valid) {
    throw new AgentEventValidationError("agent_event_generation_ids_invalid");
  }
}

export function parseAgentEventEnvelope(
  eventName: string,
  value: unknown,
): AgentEventEnvelope {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new AgentEventValidationError("agent_event_not_object");
  }
  const item = value as Record<string, unknown>;
  assertEventScope(eventName, item);
  assertOutputPayload(eventName, item);
  assertRevisionPayload(item);
  assertTerminalPayload(item);
  assertGenerationIds(item.generation_ids);
  return item as unknown as AgentEventEnvelope;
}
