import type { apiFetch as ApiFetch } from "./http";
declare const apiFetch: typeof ApiFetch;

// Checked by the repository tsc gate; never sends a network request.
export function headTypeContract(): void {
  const head = apiFetch<{ ok: boolean }>("/probe", { method: "HEAD" });
  const noBody: Promise<undefined> = head;
  // @ts-expect-error HEAD cannot promise a JSON body, even with an explicit generic.
  const jsonBody: Promise<{ ok: boolean }> = head;
  void head.then((body) => {
    // @ts-expect-error HEAD has no response properties.
    return body.ok;
  });
  const get: Promise<{ ok: boolean }> = apiFetch<{ ok: boolean }>("/probe", { method: "GET" });
  void [noBody, jsonBody, get];
}
