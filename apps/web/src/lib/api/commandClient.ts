import {
  identityWritePolicy,
  type IdentityWritePolicy,
} from "@/lib/auth/identityPolicy";
import { coordinateUnauthorized } from "@/lib/auth/authFailureCoordinator";
import { ApiError } from "./errors";
import { apiTransport } from "./transport";
import type { RequestBudget } from "./requestBudget";
import type { ResponseValidator } from "./response";

type CommandOptions<T = unknown> = Omit<RequestInit, "method"> & {
  method?: "POST" | "PUT" | "PATCH" | "DELETE";
  budget?: RequestBudget;
  expectNoContent?: boolean;
  validate?: ResponseValidator<T>;
};

export class CommandApiClient {
  private readonly policy: IdentityWritePolicy;

  constructor(policy: IdentityWritePolicy = identityWritePolicy) {
    this.policy = policy;
  }

  request<T>(
    path: string,
    options: CommandOptions<T> = {},
  ): Promise<T | undefined> {
    const method = options.method ?? "POST";
    try {
      this.policy.assertAllowed(method, path);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        coordinateUnauthorized();
      }
      throw error;
    }
    const headers = new Headers(options.headers);
    const idempotent =
      headers.has("Idempotency-Key") || headers.has("idempotency-key");
    return apiTransport.request<T>(path, {
      ...options,
      method,
      headers,
      requestClass: idempotent ? "idempotent-command" : "command",
      applyCsrf: true,
    });
  }

  post<TBody, TResult>(
    path: string,
    body: TBody,
    options: Omit<CommandOptions<TResult>, "method" | "body"> = {},
  ): Promise<TResult> {
    return this.request<TResult>(path, {
      ...options,
      method: "POST",
      body: JSON.stringify(body),
    }) as Promise<TResult>;
  }
}

export const commandClient = new CommandApiClient();
