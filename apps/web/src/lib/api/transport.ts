import { coordinateUnauthorized } from "@/lib/auth/authFailureCoordinator";
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
import { readResponseData, sessionCookieSecureSignal } from "./response";
import { retryModeFor } from "./retryPolicy";

export type TransportRequest = RequestInit & {
  requestClass: RequestClass;
  budget?: RequestBudget;
  expectNoContent?: boolean;
  applyCsrf?: boolean;
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
    init: TransportRequest,
  ): Promise<T | undefined> {
    const { expectNoContent = false, ...requestInit } = init;
    return this.requestRaw<T | undefined>(
      path,
      requestInit,
      async (response): Promise<T | undefined> =>
        expectNoContent
          ? undefined
          : ((await readResponseData(response)) as T),
    );
  }

  async requestRaw<T>(
    path: string,
    init: Omit<TransportRequest, "expectNoContent">,
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
      const prepared = await this.prepareRequest(
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
        throw new ApiError({
          ...parsed,
          status: response.status,
          payload: data,
        });
      }
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
    return executeFetch(apiUrl(path), init, {
      retryMode: retryModeFor((init.method ?? "GET").toUpperCase(), headers),
    });
  }

  private async prepareRequest(
    path: string,
    init: RequestInit,
    method: string,
    applyCsrf: boolean,
    signal?: AbortSignal,
  ): Promise<RequestInit> {
    const headers = requestHeaders(init);
    if (applyCsrf) await this.csrf.apply(headers, method, signal);
    return {
      ...init,
      method,
      headers,
      credentials: "include",
      signal,
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
