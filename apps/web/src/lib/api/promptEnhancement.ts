import {
  API_BASE,
  ApiError,
  ensureCsrfToken,
  handle401,
  refreshCsrfToken,
} from "./http";
import type { VideoPromptEnhanceIn } from "../types";

function createSSEDataParser(onData: (data: string) => void): {
  feed: (chunk: string) => void;
  flush: () => void;
} {
  let buffer = "";
  let dataLines: string[] = [];
  let pendingCR = false;

  const dispatch = () => {
    if (dataLines.length === 0) return;
    const data = dataLines.join("\n");
    dataLines = [];
    onData(data);
  };

  const processLine = (line: string) => {
    if (line === "") {
      dispatch();
      return;
    }
    if (line.startsWith(":")) return;

    const colonIdx = line.indexOf(":");
    const field = colonIdx === -1 ? line : line.slice(0, colonIdx);
    let value = colonIdx === -1 ? "" : line.slice(colonIdx + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "data") dataLines.push(value);
  };

  const feed = (chunk: string) => {
    let text = chunk;
    if (pendingCR) {
      if (text.startsWith("\n")) text = text.slice(1);
      pendingCR = false;
    }

    buffer += text;
    let start = 0;
    for (let i = 0; i < buffer.length; i += 1) {
      const code = buffer.charCodeAt(i);
      if (code !== 10 && code !== 13) continue;

      processLine(buffer.slice(start, i));
      if (code === 13) {
        if (i + 1 < buffer.length && buffer.charCodeAt(i + 1) === 10) {
          i += 1;
        } else if (i + 1 === buffer.length) {
          pendingCR = true;
        }
      }
      start = i + 1;
    }
    buffer = buffer.slice(start);
  };

  const flush = () => {
    if (buffer) {
      processLine(buffer);
      buffer = "";
    }
    pendingCR = false;
    dispatch();
  };

  return { feed, flush };
}

async function responsePayload(res: Response): Promise<unknown> {
  const contentType = res.headers.get("content-type") ?? "";
  return contentType.includes("application/json")
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);
}

function nestedError(value: unknown): {
  code?: unknown;
  message?: unknown;
} | null {
  if (!value || typeof value !== "object" || !("error" in value)) {
    return null;
  }
  const error = (value as { error?: unknown }).error;
  return error && typeof error === "object"
    ? (error as { code?: unknown; message?: unknown })
    : null;
}

function validationMessage(value: unknown): string | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  const first = value[0];
  if (!first || typeof first !== "object" || !("msg" in first)) return null;
  const message = (first as { msg?: unknown }).msg;
  return typeof message === "string" && message.trim() ? message : null;
}

type StreamErrorDetails = {
  code: string;
  message: string;
};

function errorDetailsFromValue(
  value: unknown,
  fallback: StreamErrorDetails,
): StreamErrorDetails {
  const direct = nestedError(value);
  if (direct) {
    return {
      code:
        typeof direct.code === "string" && direct.code.trim()
          ? direct.code
          : fallback.code,
      message:
        typeof direct.message === "string" && direct.message.trim()
          ? direct.message
          : fallback.message,
    };
  }
  if (typeof value === "string" && value.trim()) {
    return { ...fallback, message: value };
  }
  return {
    ...fallback,
    message: validationMessage(value) ?? fallback.message,
  };
}

function streamErrorDetails(
  payload: unknown,
  fallback: StreamErrorDetails,
): StreamErrorDetails {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) {
    return errorDetailsFromValue(payload, fallback);
  }
  return errorDetailsFromValue(
    (payload as { detail?: unknown }).detail,
    fallback,
  );
}

export async function streamApiErrorFromResponse(
  res: Response,
  fallbackCode: string,
): Promise<ApiError> {
  const payload = await responsePayload(res);
  const fallback = {
    code: fallbackCode,
    message: `HTTP ${res.status}`,
  };
  const details = streamErrorDetails(payload, fallback);
  const normalized =
    res.status === 413 && details.message === fallback.message
      ? {
          code: "request_too_large",
          message: "参考素材过大，请减少素材后重试",
        }
      : details;
  return new ApiError({ ...normalized, status: res.status, payload });
}

function promptEnhanceStreamErrorMessage(code: string): string {
  switch (code) {
    case "timeout":
      return "上游长时间没有返回内容，已自动停止。请稍后重试或减少参考素材。";
    case "upstream_error":
      return "上游暂时不可用，请稍后重试。";
    case "billing_failed":
      return "扣费结算失败，已停止本次优化。";
    case "internal":
      return "服务内部错误，请稍后重试。";
    default:
      return code;
  }
}

function unauthorizedError(): ApiError {
  handle401();
  return new ApiError({ code: "unauthorized", message: "未登录", status: 401 });
}

function networkError(err: unknown): ApiError {
  return new ApiError({
    code: "network_error",
    message: err instanceof Error ? err.message : "network error",
    status: 0,
  });
}

async function fetchEnhancement(
  doFetch: (csrf: string | null) => Promise<Response>,
  signal?: AbortSignal,
): Promise<Response> {
  try {
    const response = await doFetch(await ensureCsrfToken());
    if (response.status !== 403) return response;
    const error = await streamApiErrorFromResponse(response, "enhance_failed");
    if (error.code !== "csrf_failed") throw error;
    const fresh = await refreshCsrfToken().catch(() => null);
    if (!fresh) throw error;
    return await doFetch(fresh);
  } catch (err) {
    if (err instanceof ApiError || signal?.aborted) throw err;
    throw networkError(err);
  }
}

function parseEnhancementEvent(
  payload: string,
  state: { hasText: boolean; streamDone: boolean },
  onDelta: (text: string) => void,
): void {
  const data = payload.trim();
  if (data === "[DONE]") {
    state.streamDone = true;
    return;
  }
  try {
    const event = JSON.parse(data) as { text?: string; error?: string };
    if (event.error) {
      throw new ApiError({
        code: event.error,
        message: promptEnhanceStreamErrorMessage(event.error),
        status: 502,
      });
    }
    if (event.text) {
      state.hasText = true;
      onDelta(event.text);
    }
  } catch (err) {
    if (err instanceof ApiError) throw err;
    try {
      console.error("[enhancePrompt] parser error:", err);
    } catch {
      /* console 不可用时忽略 */
    }
    throw new ApiError({
      code: "enhance_parse_error",
      message: "Failed to parse enhancement response",
      status: 502,
    });
  }
}

async function consumeEnhancementStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onDelta: (text: string) => void,
): Promise<void> {
  const decoder = new TextDecoder();
  const state = { hasText: false, streamDone: false };
  const parser = createSSEDataParser((payload) =>
    parseEnhancementEvent(payload, state, onDelta),
  );
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        const tail = decoder.decode();
        if (tail) parser.feed(tail);
        parser.flush();
        break;
      }
      parser.feed(decoder.decode(value, { stream: true }));
      if (!state.streamDone) continue;
      if (!state.hasText) {
        throw new ApiError({
          code: "enhance_empty_response",
          message: "empty response",
          status: 502,
        });
      }
      try {
        await reader.cancel();
      } catch {
        // ignore
      }
      return;
    }
  } catch (err) {
    try {
      await reader.cancel();
    } catch {
      // ignore
    }
    throw err;
  }
  if (!state.hasText) {
    throw new ApiError({
      code: "enhance_empty_response",
      message: "empty response",
      status: 502,
    });
  }
}

export async function streamPromptEnhancement(
  path: string,
  body: unknown,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const url = `${API_BASE.replace(/\/$/, "")}${path}`;
  const doFetch = (csrf: string | null) =>
    fetch(url, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      },
      body: JSON.stringify(body),
      signal,
    });
  const response = await fetchEnhancement(doFetch, signal);
  if (response.status === 401) throw unauthorizedError();
  if (!response.ok) {
    throw await streamApiErrorFromResponse(response, "enhance_failed");
  }
  const reader = response.body?.getReader();
  if (!reader) {
    throw new ApiError({
      code: "enhance_empty_response",
      message: "empty response",
      status: 502,
    });
  }
  await consumeEnhancementStream(reader, onDelta);
}

export function enhancePrompt(
  text: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamPromptEnhancement("/prompts/enhance", { text }, onDelta, signal);
}

export function enhanceVideoPrompt(
  body: VideoPromptEnhanceIn,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamPromptEnhancement(
    "/prompts/video/enhance",
    body,
    onDelta,
    signal,
  );
}
