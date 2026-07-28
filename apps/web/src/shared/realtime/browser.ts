export function createBroadcastChannel(name: string): BroadcastChannel {
  if (typeof BroadcastChannel === "undefined") {
    throw new Error("BroadcastChannel is unavailable");
  }
  return new BroadcastChannel(name);
}

export function createEventSource(
  url: string,
  init: EventSourceInit,
): EventSource {
  return new EventSource(url, init);
}
