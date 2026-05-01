// BUG-028: 抽取共享的 isAbortLike / errorMessage 工具函数，
// 避免在 4 个文件中重复定义（ConversationCanvas、DesktopConversationCanvas、
// MobileConversationCanvas、MobileEmptyStudio）。

export function isAbortLike(err: unknown): boolean {
  if (err instanceof DOMException && err.name === "AbortError") return true;
  if (!(err instanceof Error)) return false;
  return err.name === "AbortError" || err.message.toLowerCase().includes("abort");
}

export function errorMessage(err: unknown): string | null {
  if (!err) return null;
  if (typeof err === "string") return err;
  if (err instanceof Error) return err.message || "消息加载失败，请重试";
  if (typeof err === "object" && err !== null) {
    const message = (err as Record<string, unknown>).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return "消息加载失败，请重试";
}
