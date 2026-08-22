import {
  Agent,
  ProxyAgent,
  Socks5ProxyAgent,
  buildConnector,
  fetch as undiciFetch,
  type Dispatcher,
} from "undici";
import { isIP } from "node:net";
import type { LookupFunction } from "node:net";

export interface ProviderTransport {
  readonly fetch: typeof globalThis.fetch;
  close(): Promise<void>;
}

function normalizedSocksUrl(proxyUrl: URL): URL {
  const normalized = new URL(proxyUrl);
  if (normalized.protocol === "socks5h:") normalized.protocol = "socks5:";
  return normalized;
}

function canonicalHost(value: string): string {
  return value.trim().replace(/^\[|\]$/gu, "").replace(/\.$/u, "").toLowerCase();
}

function pinnedLookup(expectedHost: string, resolvedIps: readonly string[]): LookupFunction {
  const expected = canonicalHost(expectedHost);
  const addresses = resolvedIps.map((address) => ({
    address,
    family: isIP(address),
  }));
  if (addresses.length === 0 || addresses.some((item) => item.family === 0)) {
    throw new Error("provider DNS pin contains an invalid address");
  }
  let cursor = 0;
  return (hostname, options, callback) => {
    if (canonicalHost(hostname) !== expected) {
      callback(new Error("pinned provider host mismatch"), "", 0);
      return;
    }
    const requestedFamily =
      typeof options === "number" ? options : Number(options.family ?? 0);
    const candidates = addresses.filter(
      (item) => requestedFamily === 0 || item.family === requestedFamily,
    );
    if (candidates.length === 0) {
      callback(new Error("pinned provider address family unavailable"), "", 0);
      return;
    }
    if (typeof options === "object" && options.all === true) {
      callback(null, candidates);
      return;
    }
    const selected = candidates[cursor % candidates.length];
    if (selected === undefined) {
      callback(new Error("pinned provider address unavailable"), "", 0);
      return;
    }
    cursor += 1;
    callback(null, selected.address, selected.family);
  };
}

function dispatcherFor(
  proxyUrl: string | null,
  baseUrl: string,
  resolvedIps: readonly string[],
): Dispatcher {
  if (proxyUrl === null) {
    const hostname = new URL(baseUrl).hostname;
    const connect =
      resolvedIps.length > 0
        ? buildConnector({ lookup: pinnedLookup(hostname, resolvedIps) })
        : undefined;
    return new Agent({
      connections: 8,
      pipelining: 1,
      allowH2: false,
      ...(connect ? { connect } : {}),
    });
  }
  const parsed = new URL(proxyUrl);
  if (parsed.protocol === "http:" || parsed.protocol === "https:") {
    return new ProxyAgent({ uri: parsed.toString(), allowH2: false });
  }
  if (parsed.protocol === "socks5:" || parsed.protocol === "socks5h:") {
    return new Socks5ProxyAgent(normalizedSocksUrl(parsed), {
      connections: 8,
      pipelining: 1,
    });
  }
  throw new Error("unsupported provider proxy protocol");
}

export function createProviderTransport(
  proxyUrl: string | null,
  baseUrl: string,
  resolvedIps: readonly string[],
  onDispatch: () => Promise<void>,
): ProviderTransport {
  const dispatcher = dispatcherFor(proxyUrl, baseUrl, resolvedIps);
  const allowedOrigin = new URL(baseUrl).origin;
  const fetch: typeof globalThis.fetch = async (input, init) => {
    const requestInput =
      typeof input === "string" || input instanceof URL ? input : input.url;
    const requestUrl = new URL(requestInput);
    if (requestUrl.origin !== allowedOrigin) {
      throw new Error("provider request origin does not match the configured endpoint");
    }
    if (init?.redirect !== undefined && init.redirect !== "manual" && init.redirect !== "error") {
      throw new Error("provider redirects must not be followed");
    }
    await onDispatch();
    const requestInit = {
      ...(init ?? {}),
      dispatcher,
      redirect: "manual",
    } as unknown as Parameters<typeof undiciFetch>[1];
    const response = (await undiciFetch(requestUrl, requestInit)) as unknown as Response;
    if (response.status >= 300 && response.status < 400) {
      await response.body?.cancel();
      throw new Error("provider redirect rejected");
    }
    return response;
  };
  return {
    fetch,
    async close(): Promise<void> {
      await dispatcher.close();
    },
  };
}
