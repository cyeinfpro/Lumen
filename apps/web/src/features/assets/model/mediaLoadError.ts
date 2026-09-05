export function mediaLoadError(error?: unknown): { title: string; detail: string; retryable: boolean } {
  const status = error && typeof error === "object" && "status" in error ? error.status : undefined;
  if (status === 403) {
    return { title: "访问受限", detail: "当前账户没有访问权限。", retryable: false };
  }
  if (status === 401) {
    return { title: "登录已失效", detail: "重新登录后可查看记录。", retryable: false };
  }
  if (status === 0 || (error instanceof Error && /network|fetch|timeout|网络|超时/i.test(error.message))) {
    return { title: "网络连接异常", detail: "记录未能更新，已加载内容仍保留。", retryable: true };
  }
  return { title: "记录加载失败", detail: "服务暂不可用，已加载内容仍保留。", retryable: true };
}
