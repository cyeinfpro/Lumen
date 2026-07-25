import { identityWritePolicy } from "@/lib/auth/identityPolicy";
import { apiTransport } from "./transport";
import type { RequestBudget } from "./requestBudget";

type UploadOptions = Omit<RequestInit, "method" | "body"> & {
  method?: "POST" | "PUT" | "PATCH";
  budget?: RequestBudget;
};

export const uploadClient = {
  send<TResult>(
    path: string,
    body: FormData | Blob,
    options: UploadOptions = {},
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
