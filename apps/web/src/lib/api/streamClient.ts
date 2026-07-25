import { apiUrl } from "./baseUrl";
import { NO_DEADLINE } from "./requestBudget";
import { apiTransport } from "./transport";

export const streamClient = {
  url(path: string): string {
    return apiUrl(path);
  },
  postJson(
    path: string,
    body: unknown,
    signal?: AbortSignal,
  ): Promise<Response> {
    return apiTransport.requestRaw<Response>(
      path,
      {
        method: "POST",
        body: JSON.stringify(body),
        signal,
        requestClass: "command",
        budget: NO_DEADLINE,
        applyCsrf: true,
      },
      async (response) => response,
    );
  },
};
