import { ApiError } from "./errors";

export type ResponseValidator<T> = (value: unknown) => T;

function isJsonContentType(value: string): boolean {
  const mediaType = value.split(";", 1)[0]?.trim().toLowerCase() ?? "";
  return mediaType === "application/json" || mediaType.endsWith("+json");
}

export async function readErrorResponseData(res: Response): Promise<unknown> {
  if (res.status === 204) return undefined;
  const contentType = res.headers.get("content-type") ?? "";
  return isJsonContentType(contentType)
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);
}

export async function readSuccessResponseData(res: Response): Promise<unknown> {
  if (res.status === 204) return undefined;
  const contentType = res.headers.get("content-type") ?? "";
  if (!isJsonContentType(contentType)) {
    throw new ApiError({
      code: "response_content_type_error",
      message: "Successful API response is not JSON",
      status: res.status,
      payload: {
        content_type: contentType || null,
      },
    });
  }
  let data: unknown;
  try {
    data = await res.json();
  } catch (error) {
    throw responseParseError(res, "successful response contains invalid JSON", {
      cause: error,
      content_type: contentType,
    });
  }
  if (data === null) {
    throw new ApiError({
      code: "response_schema_error",
      message: "Successful typed API response contains JSON null",
      status: res.status,
      payload: {
        content_type: contentType,
      },
    });
  }
  return data;
}

export function applyResponseValidator<T>(
  res: Response,
  path: string,
  data: unknown,
  validate: ResponseValidator<T>,
): T {
  try {
    return validate(data);
  } catch (error) {
    if (error instanceof ApiError && error.code === "response_schema_error") {
      throw error;
    }
    throw new ApiError({
      code: "response_schema_error",
      message: `API response schema validation failed for ${path}`,
      status: res.status,
      payload: {
        path,
        cause: error instanceof Error ? error.message : String(error),
      },
    });
  }
}

function responseParseError(
  res: Response,
  message: string,
  payload: unknown,
): ApiError {
  return new ApiError({
    code: "response_parse_error",
    message,
    status: res.status,
    payload,
  });
}

export const readResponseData = readErrorResponseData;

export function sessionCookieSecureSignal(
  res: Response,
  data: unknown,
): boolean | null {
  const header = res.headers.get("x-lumen-session-cookie-secure")?.trim();
  if (header === "1" || header === "true") return true;
  if (header === "0" || header === "false") return false;
  if (!data || typeof data !== "object") return null;
  const direct = (data as { session_cookie_secure?: unknown })
    .session_cookie_secure;
  if (typeof direct === "boolean") return direct;
  const detail = (data as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const nested = (detail as { session_cookie_secure?: unknown })
    .session_cookie_secure;
  return typeof nested === "boolean" ? nested : null;
}
