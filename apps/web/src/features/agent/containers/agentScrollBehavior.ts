export function preferredAgentScrollBehavior(requestSmooth: boolean): ScrollBehavior {
  const reduced = typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return requestSmooth && !reduced ? "smooth" : "auto";
}

export function agentContentScrollAction(input: {
  hasContent: boolean;
  hasPrependAnchor: boolean;
  pinned: boolean;
  newLocalSubmission: boolean;
}): "prepend" | "latest" | "notify" | "none" {
  if (input.hasPrependAnchor) return "prepend";
  if (!input.hasContent) return "none";
  if (input.newLocalSubmission || input.pinned) return "latest";
  return "notify";
}

export function observeAgentContentResize(input: {
  root: HTMLElement;
  content: HTMLElement;
  canFollow: () => boolean;
}): () => void {
  if (typeof ResizeObserver === "undefined") return () => undefined;
  const observer = new ResizeObserver(() => {
    if (!input.canFollow()) return;
    input.root.scrollTo({ top: input.root.scrollHeight, behavior: "auto" });
  });
  // Content changes include disclosures/media; viewport changes include composer padding.
  observer.observe(input.content);
  observer.observe(input.root);
  return () => observer.disconnect();
}
