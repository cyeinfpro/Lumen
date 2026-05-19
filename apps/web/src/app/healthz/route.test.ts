import { deepEqual, equal } from "node:assert/strict";
import { test } from "node:test";

type HealthRouteModule = {
  GET: () => Response | Promise<Response>;
  HEAD: () => Response | Promise<Response>;
};

async function loadRoute(): Promise<HealthRouteModule> {
  return (await import(
    new URL("./route.ts", import.meta.url).href
  )) as HealthRouteModule;
}

test("GET returns a local web health response", async () => {
  const route = await loadRoute();
  const response = await route.GET();

  equal(response.status, 200);
  equal(response.headers.get("cache-control"), "no-store");
  deepEqual(await response.json(), {
    status: "ok",
    service: "web",
  });
});

test("HEAD returns success without a response body", async () => {
  const route = await loadRoute();
  const response = await route.HEAD();

  equal(response.status, 204);
  equal(response.headers.get("cache-control"), "no-store");
  equal(await response.text(), "");
});
