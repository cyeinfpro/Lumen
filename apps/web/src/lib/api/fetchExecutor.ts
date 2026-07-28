import {
  retryDelayMs,
  shouldRetryStatus,
  waitForRetry,
  type RetryMode,
} from "./retryPolicy";

export type FetchExecutorOptions = {
  retryMode: RetryMode;
  maxRetries?: number;
  fetchImpl?: typeof fetch;
};

export type RequestFactory = () => RequestInit;

export async function executeFetch(
  url: string,
  requestFactory: RequestFactory,
  options: FetchExecutorOptions,
): Promise<Response> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const maxRetries = options.maxRetries ?? 2;
  let attempt = 0;

  while (true) {
    const init = requestFactory();
    try {
      const response = await fetchImpl(url, init);
      if (
        options.retryMode === "none" ||
        attempt >= maxRetries ||
        !shouldRetryStatus(response.status)
      ) {
        return response;
      }
      await response.body?.cancel().catch(() => undefined);
      await waitForRetry(retryDelayMs(attempt, response), init.signal);
    } catch (error) {
      if (init.signal?.aborted) throw error;
      if (options.retryMode === "none" || attempt >= maxRetries) throw error;
      await waitForRetry(retryDelayMs(attempt), init.signal);
    }
    attempt += 1;
  }
}
