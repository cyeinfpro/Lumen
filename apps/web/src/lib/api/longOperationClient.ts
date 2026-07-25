import { identityWritePolicy } from "@/lib/auth/identityPolicy";
import { apiTransport } from "./transport";
import type { RequestBudget } from "./requestBudget";

type LongOperationOptions = Omit<RequestInit, "method"> & {
  method?: "POST" | "PUT" | "PATCH" | "DELETE";
  budget: RequestBudget;
};

export const longOperationClient = {
  run<TResult>(
    path: string,
    options: LongOperationOptions,
  ): Promise<TResult> {
    const method = options.method ?? "POST";
    identityWritePolicy.assertAllowed(method, path);
    return apiTransport.request<TResult>(path, {
      ...options,
      method,
      requestClass: "long-operation",
      applyCsrf: true,
    }) as Promise<TResult>;
  },
};
