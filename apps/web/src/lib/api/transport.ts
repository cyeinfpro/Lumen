import { coordinateUnauthorized } from "@/lib/auth/authFailureCoordinator";
import {
  applyConfirmedIdentityHeader,
  assertConfirmedIdentityResponse,
  coordinateIdentityMismatchResponse,
} from "@/lib/auth/identityPolicy";
import { apiUrl } from "./baseUrl";
import { csrfService, type CsrfService } from "./csrf";
import {
  ApiError,
  networkError,
  parseApiError,
} from "./errors";
import { executeFetch } from "./fetchExecutor";
import {
  budgetFor,
  type RequestBudget,
  type RequestClass,
} from "./requestBudget";
import { createRequestSignal } from "./requestSignal";
import {
  applyResponseValidator,
  readResponseData,
  readSuccessResponseData,
  sessionCookieSecureSignal,
  type ResponseValidator,
} from "./response";
import {
  isReplayableBody,
  retryModeFor,
} from "./retryPolicy";

export type TransportRequest<T = unknown> = RequestInit & {
  requestClass: RequestClass;
  budget?: RequestBudget;
  expectNoContent?: boolean;
  applyCsrf?: boolean;
  validate?: ResponseValidator<T>;
};

function isBinaryBody(body: BodyInit | null | undefined): boolean {
  return (
    (typeof FormData !== "undefined" && body instanceof FormData) ||
    (typeof Blob !== "undefined" && body instanceof Blob) ||
    (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) ||
    (typeof ArrayBuffer !== "undefined" &&
      (body instanceof ArrayBuffer ||
        ArrayBuffer.isView(body as ArrayBufferView)))
  );
}

function requestHeaders(init: RequestInit): Headers {
  const headers = new Headers(init.headers ?? {});
  if (init.body && !isBinaryBody(init.body) && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  return headers;
}

function cloneRequestBody(
  body: BodyInit | null | undefined,
): BodyInit | null | undefined {
  if (typeof FormData !== "undefined" && body instanceof FormData) {
    const clone = new FormData();
    for (const [name, value] of body.entries()) {
      if (typeof File !== "undefined" && value instanceof File) {
        clone.append(name, value, value.name);
      } else {
        clone.append(name, value);
      }
    }
    return clone;
  }
  if (typeof Blob !== "undefined" && body instanceof Blob) {
    return body.slice(0, body.size, body.type);
  }
  if (
    typeof URLSearchParams !== "undefined" &&
    body instanceof URLSearchParams
  ) {
    return new URLSearchParams(body);
  }
  return body;
}

function requestFactory(init: RequestInit): () => RequestInit {
  return () => ({
    ...init,
    headers: new Headers(init.headers),
    body: cloneRequestBody(init.body),
  });
}

function unauthorizedError(response: Response, data: unknown): ApiError {
  coordinateUnauthorized();
  const secure = sessionCookieSecureSignal(response, data);
  return new ApiError({
    code: "unauthorized",
    message: "未登录或会话已失效",
    status: 401,
    payload:
      secure === null
        ? data
        : { response: data, session_cookie_secure: secure },
  });
}

export class ApiTransport {
  private readonly csrf: CsrfService;

  constructor(csrf: CsrfService = csrfService) {
    this.csrf = csrf;
  }

  async request<T>(
    path: string,
    init: TransportRequest<T>,
  ): Promise<T | undefined> {
    const {
      expectNoContent = false,
      validate,
      ...requestInit
    } = init;
    return this.requestRaw<T | undefined>(
      path,
      requestInit,
      async (response): Promise<T | undefined> => {
        if (expectNoContent) return undefined;
        const data = await readSuccessResponseData(response);
        return validate
          ? applyResponseValidator(response, path, data, validate)
          : (data as T);
      },
    );
  }

  async requestRaw<T>(
    path: string,
    init: Omit<TransportRequest<T>, "expectNoContent" | "validate">,
    readSuccess: (response: Response) => Promise<T>,
    readError: (response: Response) => Promise<unknown> = readResponseData,
  ): Promise<T> {
    const { requestClass, budget, applyCsrf = false, ...requestInit } = init;
    const method = (requestInit.method ?? "GET").toUpperCase();
    const deadline = createRequestSignal(
      requestInit.signal,
      budgetFor(requestClass, budget),
    );
    try {
      const { identity, request: prepared } = await this.prepareRequest(
        path,
        requestInit,
        method,
        applyCsrf,
        deadline.signal,
      );
      let { response, data } = await this.fetchData(
        path,
        prepared,
        readSuccess,
        readError,
      );
      ({ response, data } = await this.retryCsrf(
        path,
        prepared,
        response,
        data,
        applyCsrf,
        deadline.signal,
        readSuccess,
        readError,
      ));
      deadline.throwIfAborted();
      if (response.status === 401) throw unauthorizedError(response, data);
      if (!response.ok) {
        const parsed = parseApiError(response.status, data);
        coordinateIdentityMismatchResponse(response.status, data);
        throw new ApiError({
          ...parsed,
          status: response.status,
          payload: data,
        });
      }
      assertConfirmedIdentityResponse(identity);
      return data as T;
    } catch (error) {
      deadline.throwIfAborted(error);
      if (error instanceof ApiError) throw error;
      if (error instanceof Error && error.name === "AbortError") throw error;
      throw networkError(error);
    } finally {
      deadline.cleanup();
    }
  }

  private fetch(path: string, init: RequestInit): Promise<Response> {
    const headers = new Headers(init.headers);
    return executeFetch(apiUrl(path), requestFactory(init), {
      retryMode: retryModeFor(
        (init.method ?? "GET").toUpperCase(),
        headers,
        init.body,
      ),
    });
  }

  private async prepareRequest(
    path: string,
    init: RequestInit,
    method: string,
    applyCsrf: boolean,
    signal?: AbortSignal,
  ) {
    const headers = requestHeaders(init);
    const identity = applyConfirmedIdentityHeader(headers, path);
    if (applyCsrf) await this.csrf.apply(headers, method, signal);
    return {
      identity,
      request: {
        ...init,
        method,
        headers,
        credentials: "include",
        signal,
      } satisfies RequestInit,
    };
  }

  private async fetchData(
    path: string,
    init: RequestInit,
    readSuccess: (response: Response) => Promise<unknown>,
    readError: (response: Response) => Promise<unknown>,
  ): Promise<{ response: Response; data: unknown }> {
    const response = await this.fetch(path, init);
    return {
      response,
      data: await (response.ok ? readSuccess(response) : readError(response)),
    };
  }

  private async retryCsrf(
    path: string,
    init: RequestInit,
    response: Response,
    data: unknown,
    applyCsrf: boolean,
    signal?: AbortSignal,
    readSuccess: (response: Response) => Promise<unknown> = readResponseData,
    readError: (response: Response) => Promise<unknown> = readResponseData,
  ): Promise<{ response: Response; data: unknown }> {
    if (
      response.status !== 403 ||
      !applyCsrf ||
      !isReplayableBody(init.body) ||
      parseApiError(response.status, data).code !== "csrf_failed"
    ) {
      return { response, data };
    }
    const token = await this.csrf.refresh(signal).catch(() => null);
    if (!token) return { response, data };
    const headers = new Headers(init.headers);
    headers.set("x-csrf-token", token);
    return this.fetchData(
      path,
      { ...init, headers },
      readSuccess,
      readError,
    );
  }
}

export const apiTransport = new ApiTransport();
