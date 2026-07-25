import { apiTransport } from "./transport";
import { deadline, type RequestBudget } from "./requestBudget";

type DownloadOptions = Omit<RequestInit, "method"> & {
  budget?: RequestBudget;
};

export const downloadClient = {
  postBlob(
    path: string,
    options: DownloadOptions = {},
  ): Promise<Blob> {
    return apiTransport.requestRaw<Blob>(
      path,
      {
        ...options,
        method: "POST",
        requestClass: "long-operation",
        budget: options.budget ?? deadline(5 * 60_000),
        applyCsrf: true,
      },
      (response) => response.blob(),
    );
  },
};
