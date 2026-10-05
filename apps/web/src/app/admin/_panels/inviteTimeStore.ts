// A snapshot changes only when subscribers are notified, never during render.
export function createInviteTimeStore() {
  let snapshot = 0;
  const listeners = new Set<() => void>();
  let timer: ReturnType<typeof setInterval> | undefined;
  const update = () => {
    snapshot = Date.now();
    listeners.forEach((listener) => listener());
  };
  return {
    getSnapshot: () => snapshot,
    getServerSnapshot: () => 0,
    subscribe(listener: () => void) {
      listeners.add(listener);
      if (listeners.size === 1) {
        update();
        timer = setInterval(update, 60_000);
      }
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) {
          clearInterval(timer);
          timer = undefined;
        }
      };
    },
  };
}

export const inviteTimeStore = createInviteTimeStore();
