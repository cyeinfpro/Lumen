import { equal, ok } from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import ts from "typescript";

function loadStatus(overrides: Record<string, unknown>) {
  const source = readFileSync(new URL("./RuntimeResilienceStatus.tsx", import.meta.url), "utf8");
  const output = ts.transpileModule(source, { fileName: "RuntimeResilienceStatus.tsx", compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
    jsx: ts.JsxEmit.React, jsxFactory: "element",
  } }).outputText;
  const compiledModule = { exports: {} };
  new Function("require", "module", "exports", "element", output)(
    (id: string) => { if (!(id in overrides)) throw new Error("Unexpected dependency: " + id); return overrides[id]; },
    compiledModule, compiledModule.exports,
    (type: unknown, props: Record<string, unknown> | null, ...children: unknown[]) => ({ type, props: { ...props, children } }),
  );
  return compiledModule.exports as Record<string, () => Element | null>;
}

type Element = { type: unknown; props: Record<string, unknown> };
function buttonIn(value: unknown): Element | null {
  if (!value || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (const child of value) { const button = buttonIn(child); if (button) return button; }
    return null;
  }
  if (!("props" in value)) return null;
  const element = value as Element;
  return element.type === "button" ? element : buttonIn(element.props.children);
}

for (const component of ["RuntimeResilienceStatus", "MobileRuntimeResilienceStatus"]) {
  test(component + " retains a focusable, busy recovery control without duplicate requests", () => {
    let session = "degraded";
    let recoveries = 0;
    let logins = 0;
    let recovery: { requested: boolean; status: string } | null = null;
    const compiledModule = loadStatus({
      react: {
        useState: (initial: { requested: boolean; status: string }) => {
          recovery ??= initial;
          return [recovery, (value: { requested: boolean; status: string }) => { recovery = value; }];
        },
      },
      "lucide-react": { AlertTriangle: "i", LogIn: "i", RefreshCw: "i" },
      "@/components/ui/primitives": { IconButton: "button" },
      "@/lib/auth/navigation": { replaceWithLogin: () => { logins += 1; } },
      "@/lib/runtimeResilience": {
        useRuntimeResilience: () => ({ session, realtime: "idle" }),
        requestRuntimeRecovery: () => { recoveries += 1; },
      },
    });
    const render = compiledModule[component]!;
    session = "revalidating";
    equal(render(), null, "initial/background verification stays quiet");
    session = "degraded";
    const failed = buttonIn(render());
    ok(failed);
    (failed.props.onClick as () => void)();
    equal(recoveries, 1);

    session = "revalidating";
    const pendingTree = render();
    ok(pendingTree);
    equal(pendingTree.props.role, "status");
    equal(pendingTree.props["aria-live"], "polite");
    const pending = buttonIn(pendingTree);
    ok(pending);
    equal(pending.type, failed.type);
    equal(pending.props["aria-busy"], true);
    equal(pending.props["aria-disabled"], true);
    equal(pending.props.disabled, undefined, "native disabling would discard keyboard focus");
    (pending.props.onClick as () => void)();
    (pending.props.onClick as () => void)();
    equal(recoveries, 1);

    session = "degraded";
    const retry = buttonIn(render());
    ok(retry);
    equal(retry.props["aria-disabled"], undefined);
    (retry.props.onClick as () => void)();
    equal(recoveries, 2);

    session = "unauthorized";
    const login = buttonIn(render());
    ok(login);
    (login.props.onClick as () => void)();
    equal(logins, 1);
    equal(recoveries, 2);
    for (session of ["public", "unknown", "authenticated"]) equal(render(), null);
  });
}
