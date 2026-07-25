import { apiTransport } from "./transport";
import type { RequestBudget } from "./requestBudget";

type QueryOptions = Omit<RequestInit, "method" | "body"> & {
  budget?: RequestBudget;
};

export const queryClient = {
  get<T>(path: string, options: QueryOptions = {}): Promise<T> {
    return apiTransport.request<T>(path, {
      ...options,
      method: "GET",
      requestClass: "query",
    }) as Promise<T>;
  },
  head<T = unknown>(
    path: string,
    options: QueryOptions = {},
  ): Promise<T | undefined> {
    return apiTransport.request<T>(path, {
      ...options,
      method: "HEAD",
      requestClass: "query",
    });
  },
};
