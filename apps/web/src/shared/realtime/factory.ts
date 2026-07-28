import {
  BrowserEventSourceTransport,
  type EventStreamTransport,
} from "@/lib/sse/eventSourceTransport";
import {
  RealtimeRuntime,
  type RealtimeRuntimeOptions,
} from "@/lib/sse/runtime";

export type CreateRealtimeRuntimeOptions = Omit<
  RealtimeRuntimeOptions,
  "transport"
> & {
  transport?: EventStreamTransport;
};

export function createRealtimeRuntime(
  options: CreateRealtimeRuntimeOptions,
): RealtimeRuntime {
  return new RealtimeRuntime({
    ...options,
    transport: options.transport ?? new BrowserEventSourceTransport(),
  });
}
