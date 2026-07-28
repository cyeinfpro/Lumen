import {
  BrowserEventSourceTransport,
  type EventStreamTransport,
} from "@/features/realtime/api/eventSourceTransport";
import {
  RealtimeRuntime,
  type RealtimeRuntimeOptions,
} from "@/features/realtime/model/runtime";

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
