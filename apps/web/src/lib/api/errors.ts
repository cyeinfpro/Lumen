export class ApiError extends Error {
  code: string;
  status: number;
  payload?: unknown;

  constructor(opts: {
    code: string;
    message: string;
    status: number;
    payload?: unknown;
  }) {
    super(opts.message);
    this.name = "ApiError";
    this.code = opts.code;
    this.status = opts.status;
    this.payload = opts.payload;
  }
}

export type ParsedApiError = {
  code: string;
  message: string;
};

function parseErrorObject(value: unknown): Partial<ParsedApiError> | null {
  if (!value || typeof value !== "object" || !("error" in value)) return null;
  const error = (value as { error?: unknown }).error;
  if (!error || typeof error !== "object") return null;
  const record = error as { code?: unknown; message?: unknown };
  return {
    code: typeof record.code === "string" ? record.code : undefined,
    message: typeof record.message === "string" ? record.message : undefined,
  };
}

function validationMessage(detail: unknown): string | null {
  if (!Array.isArray(detail) || detail.length === 0) return null;
  const first = detail[0];
  if (!first || typeof first !== "object" || !("msg" in first)) return null;
  const message = (first as { msg?: unknown }).msg;
  return typeof message === "string" && message.trim() ? message : null;
}

export function parseApiError(
  status: number,
  data: unknown,
): ParsedApiError {
  const fallback = { code: "http_error", message: `HTTP ${status}` };
  const direct = parseErrorObject(data);
  if (direct) return completeError(direct, fallback);
  if (data && typeof data === "object" && "detail" in data) {
    return normalizeLargeError(
      status,
      detailError((data as { detail?: unknown }).detail, fallback),
      fallback,
    );
  }
  if (typeof data === "string" && data.trim()) {
    return { ...fallback, message: data.trim() };
  }
  return normalizeLargeError(status, fallback, fallback);
}

function completeError(
  error: Partial<ParsedApiError>,
  fallback: ParsedApiError,
): ParsedApiError {
  return {
    code: error.code ?? fallback.code,
    message: error.message ?? fallback.message,
  };
}

function detailError(
  detail: unknown,
  fallback: ParsedApiError,
): ParsedApiError {
  const nested = parseErrorObject(detail);
  if (nested) return completeError(nested, fallback);
  if (typeof detail === "string" && detail.trim()) {
    return { ...fallback, message: detail };
  }
  return {
    ...fallback,
    message: validationMessage(detail) ?? fallback.message,
  };
}

function normalizeLargeError(
  status: number,
  error: ParsedApiError,
  fallback: ParsedApiError,
): ParsedApiError {
  if (status !== 413 || error.message !== fallback.message) return error;
  return {
    code: "request_too_large",
    message: "上传文件过大，请压缩后重试",
  };
}

export function networkError(error: unknown): ApiError {
  return new ApiError({
    code: "network_error",
    message: error instanceof Error ? error.message : "network error",
    status: 0,
    payload: error,
  });
}

export function timeoutError(error?: unknown): ApiError {
  return new ApiError({
    code: "request_timeout",
    message: "请求超时，服务器可能仍在处理，请稍后确认结果",
    status: 0,
    payload: error,
  });
}
