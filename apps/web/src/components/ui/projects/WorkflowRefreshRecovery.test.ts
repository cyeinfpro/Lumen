import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

type Element = { type: unknown; props: Record<string, unknown> };
type Exports = Record<string, unknown>;
const jsx = (type: unknown, props: Record<string, unknown>): Element => ({ type, props });

function elements(value: unknown): Element[] {
  if (Array.isArray(value)) return value.flatMap(elements);
  if (!value || typeof value !== "object" || !("props" in value)) return [];
  const element = value as Element;
  return [element, ...elements(element.props.children)];
}

function named(tree: Element, name: string): Element | undefined {
  return elements(tree).find((element) =>
    typeof element.type === "function" && element.type.name === name);
}

function compile(path: string, require: (id: string) => unknown): Exports {
  const url = new URL(path, import.meta.url);
  const output = ts.transpileModule(readFileSync(url, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX },
    fileName: url.pathname,
  }).outputText;
  const module = { exports: {} as Exports };
  new Function("require", "module", "exports", output)(require, module, module.exports);
  return module.exports;
}

for (const [pageName, consoleName] of [
  ["ApparelWorkflowDetail", "ProjectConsole"],
  ["PosterWorkflowDetail", "PosterConsole"],
]) {
  const load = (query: Record<string, unknown>) => compile(`./${pageName}.tsx`, (id) => {
    if (id === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "fragment" };
    if (id === "@/lib/queries") return {
      useWorkflowQuery: () => query, usePosterWorkflowQuery: () => query,
    };
    if (id === "./types") return { STATUS_LABEL: { running: "运行中" } };
    if (id === "./components/ProjectRefreshNotice") return {
      ProjectRefreshNotice: function ProjectRefreshNotice() { return null; },
    };
    return {};
  })[pageName] as (props: { projectId: string }) => Element;

  test(`${pageName} retains the console and current draft after background errors`, () => {
    const workflow = { id: "run", status: "running" };
    let retries = 0;
    const query = { data: workflow, isLoading: false, isFetching: false,
      isError: false, error: null as Error | null, refetch: () => { retries += 1; } };
    const Page = load(query);
    const initial = named(Page({ projectId: "run" }), consoleName);
    assert.ok(initial);
    query.isError = true;
    query.error = new Error("temporary sync failure");
    const tree = Page({ projectId: "run" });
    const retained = named(tree, consoleName);
    assert.ok(retained, "background errors must not unmount the editing console");
    assert.equal(retained.type, initial.type);
    assert.equal(retained.props.workflow, workflow);
    assert.equal(named(tree, "DetailError"), undefined);
    const notice = named(tree, "ProjectRefreshNotice");
    assert.ok(notice);
    (notice.props.onRetry as () => void)();
    assert.equal(retries, 1);
  });

  test(`${pageName} still provides the initial-load error retry`, () => {
    const Page = load({ data: undefined, isLoading: false, isError: true,
      error: new Error("initial failure"), refetch: () => undefined });
    const tree = Page({ projectId: "run" });
    assert.ok(named(tree, "DetailError"));
    assert.equal(named(tree, consoleName), undefined);
  });
}

test("refresh notice is quiet when healthy and prevents duplicate retries while fetching", () => {
  const module = compile("./components/ProjectRefreshNotice.tsx", (id) => {
    if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
    throw new Error(`Unexpected dependency: ${id}`);
  });
  const Notice = module.ProjectRefreshNotice as (props: {
    error: unknown; refreshing: boolean; onRetry: () => void;
  }) => Element | null;
  assert.equal(Notice({ error: null, refreshing: false, onRetry: () => undefined }), null);
  const tree = Notice({ error: new Error("offline"), refreshing: true, onRetry: () => undefined });
  assert.ok(tree);
  assert.equal(tree.props.role, "status");
  const button = elements(tree).find((element) => element.type === "button");
  assert.ok(button);
  assert.equal(button.props.disabled, true);
  assert.equal(button.props["aria-label"], "重新同步项目状态");
});
