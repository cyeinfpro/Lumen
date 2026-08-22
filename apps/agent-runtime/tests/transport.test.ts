import { createServer } from "node:http";
import { once } from "node:events";
import type { AddressInfo } from "node:net";
import { describe, expect, it } from "vitest";

import { createProviderTransport } from "../src/providers/transport.js";

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
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address() as AddressInfo;
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
      server.close();
      await once(server, "close");
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
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address() as AddressInfo;
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
      server.close();
      await once(server, "close");
    }
  });
});
