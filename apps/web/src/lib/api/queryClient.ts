import { apiTransport } from "./transport";
import type { RequestBudget } from "./requestBudget";
import type { ResponseValidator } from "./response";

type QueryOptions<T = unknown> = Omit<RequestInit, "method" | "body"> & {
  budget?: RequestBudget;
  validate?: ResponseValidator<T>;
};

export const queryClient = {
  get<T>(path: string, options: QueryOptions<T> = {}): Promise<T> {
    return apiTransport.request<T>(path, {
      ...options,
      method: "GET",
      requestClass: "query",
    }) as Promise<T>;
  },
  head<T = unknown>(
    path: string,
    options: QueryOptions<T> = {},
  ): Promise<T | undefined> {
    return apiTransport.request<T>(path, {
      ...options,
      method: "HEAD",
      requestClass: "query",
      expectNoContent: true,
    });
  },
};
