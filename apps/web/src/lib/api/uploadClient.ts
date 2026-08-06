import { identityWritePolicy } from "@/lib/auth/identityPolicy";
import { apiTransport } from "./transport";
import type { RequestBudget } from "./requestBudget";
import type { ResponseValidator } from "./response";

type UploadOptions<T = unknown> = Omit<RequestInit, "method" | "body"> & {
  method?: "POST" | "PUT" | "PATCH";
  budget?: RequestBudget;
  validate?: ResponseValidator<T>;
};

export const uploadClient = {
  send<TResult>(
    path: string,
    body: FormData | Blob,
    options: UploadOptions<TResult> = {},
  ): Promise<TResult> {
    const method = options.method ?? "POST";
    identityWritePolicy.assertAllowed(method, path);
    return apiTransport.request<TResult>(path, {
      ...options,
      method,
      body,
      requestClass: "upload",
      applyCsrf: true,
    }) as Promise<TResult>;
  },
};
