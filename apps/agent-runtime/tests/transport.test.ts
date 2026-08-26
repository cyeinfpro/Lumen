import { createServer, type IncomingMessage } from "node:http";
import { once } from "node:events";
import type { AddressInfo } from "node:net";
import { describe, expect, it } from "vitest";

import { createProviderTransport } from "../src/providers/transport.js";

async function listen(server: ReturnType<typeof createServer>): Promise<AddressInfo> {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server.address() as AddressInfo;
}

async function closeServer(server: ReturnType<typeof createServer>): Promise<void> {
  if (!server.listening) return;
  server.close();
  await once(server, "close");
}

async function readRequestBody(request: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of request as AsyncIterable<Buffer | string>) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function deferred(): { readonly promise: Promise<void>; readonly resolve: () => void } {
  let resolveValue: (() => void) | undefined;
  const promise = new Promise<void>((resolve) => {
    resolveValue = resolve;
  });
  if (resolveValue === undefined) throw new Error("deferred was not initialized");
  return { promise, resolve: resolveValue };
}

describe("Provider DNS pin transport", () => {
  it("dials an approved IP with original Host and rejects cross-host redirect", async () => {
    const hosts: string[] = [];
    const server = createServer((request, response) => {
      hosts.push(request.headers.host ?? "");
      if (request.url === "/redirect") {
        const address = server.address() as AddressInfo;
        response.writeHead(302, {
          location: `http://other.invalid:${String(address.port)}/final`,
        });
        response.end();
        return;
      }
      response.writeHead(200, { "content-type": "text/plain" });
      response.end("ok");
    });
    const address = await listen(server);
    let dispatches = 0;
    const transport = createProviderTransport(
      null,
      `http://provider.invalid:${String(address.port)}`,
      ["127.0.0.1"],
      async () => {
        dispatches += 1;
      },
    );
    try {
      const response = await transport.fetch(
        `http://provider.invalid:${String(address.port)}/ok`,
      );
      expect(await response.text()).toBe("ok");
      expect(hosts).toEqual([`provider.invalid:${String(address.port)}`]);
      expect(dispatches).toBe(1);

      await expect(
        transport.fetch(
          `http://provider.invalid:${String(address.port)}/redirect`,
        ),
      ).rejects.toThrow();
      expect(hosts).toHaveLength(2);
    } finally {
      await transport.close();
      await closeServer(server);
    }
  });

  it("rejects redirects even when the wallet provider has no DNS pin", async () => {
    const paths: string[] = [];
    const server = createServer((request, response) => {
      paths.push(request.url ?? "");
      const address = server.address() as AddressInfo;
      response.writeHead(302, {
        location: `http://localhost:${String(address.port)}/redirect-target`,
      });
      response.end();
    });
    const address = await listen(server);
    const transport = createProviderTransport(
      null,
      `http://127.0.0.1:${String(address.port)}`,
      [],
      async () => undefined,
    );
    try {
      await expect(
        transport.fetch(`http://127.0.0.1:${String(address.port)}/redirect`),
      ).rejects.toThrow(/redirect rejected/u);
      expect(paths).toEqual(["/redirect"]);
    } finally {
      await transport.close();
      await closeServer(server);
    }
  });

  it("preserves Request method, headers, body, and Fetch init override semantics", async () => {
    const observed: Array<{
      readonly method: string | undefined;
      readonly url: string | undefined;
      readonly auditHeader: string | undefined;
      readonly originalHeader: string | undefined;
      readonly body: string;
    }> = [];
    const server = createServer((request, response) => {
      void (async () => {
        observed.push({
          method: request.method,
          url: request.url,
          auditHeader: request.headers["x-audit"]?.toString(),
          originalHeader: request.headers["x-original"]?.toString(),
          body: await readRequestBody(request),
        });
        response.writeHead(200, { "content-type": "text/plain" });
        response.end("ok");
      })().catch((error: unknown) => {
        response.destroy(error instanceof Error ? error : undefined);
      });
    });
    const address = await listen(server);
    const baseUrl = `http://127.0.0.1:${String(address.port)}`;
    let dispatches = 0;
    const transport = createProviderTransport(null, baseUrl, [], async () => {
      dispatches += 1;
    });
    try {
      const inherited = new Request(`${baseUrl}/inherited`, {
        method: "POST",
        headers: {
          "content-type": "text/plain",
          "x-audit": "preserve-me",
          "x-original": "keep-me",
        },
        body: "provider-payload",
      });
      const inheritedResponse = await transport.fetch(inherited);
      expect(await inheritedResponse.text()).toBe("ok");

      const overridden = new Request(`${baseUrl}/overridden`, {
        method: "POST",
        headers: {
          "content-type": "text/plain",
          "x-original": "drop-me",
        },
        body: "original-payload",
      });
      const overriddenResponse = await transport.fetch(overridden, {
        method: "PATCH",
        headers: { "x-audit": "override-me" },
        body: "override-payload",
      });
      expect(await overriddenResponse.text()).toBe("ok");

      expect(dispatches).toBe(2);
      expect(observed).toEqual([
        {
          method: "POST",
          url: "/inherited",
          auditHeader: "preserve-me",
          originalHeader: "keep-me",
          body: "provider-payload",
        },
        {
          method: "PATCH",
          url: "/overridden",
          auditHeader: "override-me",
          originalHeader: undefined,
          body: "override-payload",
        },
      ]);
    } finally {
      await transport.close();
      await closeServer(server);
    }
  });

  it("rejects invalid origins and explicit follow redirects before dispatch", async () => {
    let dispatches = 0;
    const transport = createProviderTransport(
      null,
      "http://provider.invalid",
      [],
      async () => {
        dispatches += 1;
      },
    );
    try {
      await expect(
        transport.fetch(new Request("http://other.invalid/provider", { method: "POST" })),
      ).rejects.toThrow(/origin/u);
      await expect(
        transport.fetch("http://provider.invalid/provider", { redirect: "follow" }),
      ).rejects.toThrow(/redirects must not be followed/u);
      expect(dispatches).toBe(0);
    } finally {
      await transport.close();
    }
  });

  it("returns deterministic mock-provider 429 and 5xx responses without retrying", async () => {
    const paths: string[] = [];
    const server = createServer((request, response) => {
      paths.push(request.url ?? "");
      if (request.url === "/rate-limit") {
        response.writeHead(429, {
          "content-type": "text/plain",
          "retry-after": "7",
        });
        response.end("rate limited");
        return;
      }
      response.writeHead(503, { "content-type": "text/plain" });
      response.end("provider unavailable");
    });
    const address = await listen(server);
    const baseUrl = `http://127.0.0.1:${String(address.port)}`;
    let dispatches = 0;
    const transport = createProviderTransport(null, baseUrl, [], async () => {
      dispatches += 1;
    });
    try {
      const rateLimit = await transport.fetch(`${baseUrl}/rate-limit`);
      expect(rateLimit.status).toBe(429);
      expect(rateLimit.headers.get("retry-after")).toBe("7");
      expect(await rateLimit.text()).toBe("rate limited");

      const unavailable = await transport.fetch(`${baseUrl}/server-error`);
      expect(unavailable.status).toBe(503);
      expect(await unavailable.text()).toBe("provider unavailable");

      expect(dispatches).toBe(2);
      expect(paths).toEqual(["/rate-limit", "/server-error"]);
    } finally {
      await transport.close();
      await closeServer(server);
    }
  });

  it("surfaces an abrupt EOF while consuming a mock-provider stream", async () => {
    const server = createServer((_request, response) => {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.flushHeaders();
      response.write('data: {"partial":');
      setTimeout(() => {
        response.destroy();
      }, 10);
    });
    const address = await listen(server);
    const baseUrl = `http://127.0.0.1:${String(address.port)}`;
    let dispatches = 0;
    const transport = createProviderTransport(null, baseUrl, [], async () => {
      dispatches += 1;
    });
    try {
      await expect(
        transport.fetch(`${baseUrl}/eof`).then(async (response) => response.text()),
      ).rejects.toThrow();
      expect(dispatches).toBe(1);
    } finally {
      await transport.close();
      await closeServer(server);
    }
  });

  it("handles slow mock-provider headers and bodies", async () => {
    const server = createServer((request, response) => {
      if (request.url === "/slow-headers") {
        setTimeout(() => {
          response.writeHead(200, { "content-type": "text/plain" });
          response.end("headers-ok");
        }, 15);
        return;
      }
      response.writeHead(200, { "content-type": "text/plain" });
      response.write("slow-");
      setTimeout(() => {
        response.end("body");
      }, 15);
    });
    const address = await listen(server);
    const baseUrl = `http://127.0.0.1:${String(address.port)}`;
    let dispatches = 0;
    const transport = createProviderTransport(null, baseUrl, [], async () => {
      dispatches += 1;
    });
    try {
      const slowHeaders = await transport.fetch(`${baseUrl}/slow-headers`);
      expect(await slowHeaders.text()).toBe("headers-ok");

      const slowBody = await transport.fetch(`${baseUrl}/slow-body`);
      expect(await slowBody.text()).toBe("slow-body");

      expect(dispatches).toBe(2);
    } finally {
      await transport.close();
      await closeServer(server);
    }
  });

  it("preserves Request abort signals and closes a slow mock-provider request", async () => {
    const routeReceived = deferred();
    const responseClosed = deferred();
    let responseTimer: NodeJS.Timeout | undefined;
    const server = createServer((request, response) => {
      if (request.url !== "/abort") {
        response.writeHead(404);
        response.end();
        return;
      }
      routeReceived.resolve();
      response.on("close", () => {
        if (responseTimer !== undefined) clearTimeout(responseTimer);
        responseClosed.resolve();
      });
      responseTimer = setTimeout(() => {
        response.writeHead(200, { "content-type": "text/plain" });
        response.end("too late");
      }, 250);
    });
    const address = await listen(server);
    const baseUrl = `http://127.0.0.1:${String(address.port)}`;
    let dispatches = 0;
    const transport = createProviderTransport(null, baseUrl, [], async () => {
      dispatches += 1;
    });
    try {
      const controller = new AbortController();
      const pending = transport.fetch(
        new Request(`${baseUrl}/abort`, { signal: controller.signal }),
      );
      await routeReceived.promise;
      controller.abort();
      await expect(pending).rejects.toThrow();
      await responseClosed.promise;
      expect(dispatches).toBe(1);
    } finally {
      if (responseTimer !== undefined) clearTimeout(responseTimer);
      await transport.close();
      await closeServer(server);
    }
  });
});
