import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

function componentModule(url, extraExports = "", globals = {}) {
  const slots = [];
  let cursor = 0;
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], (value) => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }];
    },
    useEffect: (effect) => effect(),
    useRef: (value) => ({ current: value }),
    createContext: (value) => ({ value }),
    useContext: (context) => context.value,
    useMemo: (fn) => fn(),
    useSyncExternalStore: () => "",
  };
  const jsx = (type, props) => ({ type, props });
  const moduleRecord = { exports: {} };
  const source = ts.transpileModule(readFileSync(url, "utf8") + extraExports, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  runInNewContext(source, {
    ...globals,
    module: moduleRecord, exports: moduleRecord.exports,
    require(name) {
      if (name === "react") return react;
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "fragment" };
      return new Proxy({}, { get: (_object, key) => key });
    },
  });
  return { exports: moduleRecord.exports, slots, render(name, props) { cursor = 0; return moduleRecord.exports[name](props); } };
}

function descendants(node) {
  if (!node || typeof node !== "object") return [];
  return [node, ...[node.props?.children].flat(Infinity).flatMap(descendants)];
}

test("project deletion stays open until the mutation settles and reports failures locally", async () => {
  const component = componentModule(new URL("./components/ProjectDeleteDialog.tsx", import.meta.url));
  let resolve;
  const request = new Promise((done) => { resolve = done; });
  const changes = [];
  const props = { open: true, title: "Target project", pending: false, onOpenChange: (open) => changes.push(open), onConfirm: () => request };
  const dialog = component.render("ProjectDeleteDialog", props);
  assert.equal(dialog.props.title, "删除“Target project”？");
  const confirmation = dialog.props.onConfirm();
  assert.deepEqual(changes, []);
  resolve();
  await confirmation;
  assert.deepEqual(changes, [false]);

  changes.length = 0;
  await component.render("ProjectDeleteDialog", { ...props, onConfirm: async () => { throw new Error("private internal failure"); } }).props.onConfirm();
  assert.deepEqual(changes, []);
  const failed = component.render("ProjectDeleteDialog", props);
  const alert = descendants(failed.props.description).find((node) => node.props?.role === "alert");
  assert.match(alert.props.children, /删除结果未确认/);
  assert.doesNotMatch(alert.props.children, /private/);
  failed.props.onOpenChange(false);
  assert.equal(component.slots[0], null);
});

test("project deletion describes backing conversation and generated-media cleanup", () => {
  const component = componentModule(new URL("./components/ProjectDeleteDialog.tsx", import.meta.url));
  assert.match(component.exports.PROJECT_DELETE_IMPACT, /关联对话和生成图片将被移除/);
  assert.match(component.exports.PROJECT_DELETE_IMPACT, /任务将取消/);
  assert.match(component.exports.PROJECT_DELETE_IMPACT, /已保存到模特库的图片保留/);
});

test("dirty settings intercept application navigation and cancellation preserves the page", () => {
  const listeners = new Map();
  const events = { addEventListener: (name, listener) => listeners.set(name, listener), removeEventListener: () => {} };
  let navigations = 0;
  class Element { closest() { return this; } }
  class Anchor extends Element {
    isConnected = true;
    href = "http://localhost/settings/usage";
    target = "";
    hasAttribute() { return false; }
    click() { navigations += 1; }
  }
  const component = componentModule(new URL("../primitives/UnsavedSettingsGuard.tsx", import.meta.url), "", {
    document: events, window: { ...events, location: new URL("http://localhost/settings/api-key") },
    URL, Element, HTMLAnchorElement: Anchor,
  });
  component.render("UnsavedSettingsGuard", { dirty: true });
  let prevented = false;
  let stopped = false;
  listeners.get("click")({ target: new Anchor(), button: 0, preventDefault: () => { prevented = true; }, stopImmediatePropagation: () => { stopped = true; } });
  assert.equal(prevented, true);
  assert.equal(stopped, true);
  const dialog = component.render("UnsavedSettingsGuard", { dirty: true });
  assert.equal(dialog.props.open, true);
  dialog.props.onOpenChange(false);
  assert.equal(component.render("UnsavedSettingsGuard", { dirty: true }).props.open, false);
  assert.equal(navigations, 0);
  const unload = { preventDefault: () => { prevented = true; } };
  prevented = false;
  listeners.get("beforeunload")(unload);
  assert.equal(prevented, true);
  assert.equal(unload.returnValue, "");
});

test("numeric settings preserve an intentionally empty draft for field validation", () => {
  const controls = componentModule(new URL("../../../app/admin/_panels/settings/views-controls.tsx", import.meta.url), "\nexports.NumericSettingControl = NumericSettingControl;\n");
  controls.exports.SettingFieldAccessibility.value = { "aria-invalid": true, "aria-describedby": "field-help field-error" };
  let change;
  const tree = controls.render("NumericSettingControl", {
    item: { key: "numeric", value: "12" }, meta: { kind: "integer", title: "Limit" }, inputValue: "12", onChange: (value) => { change = value; },
  });
  const input = descendants(tree).find((node) => node.type === "Input");
  assert.equal(input.props["aria-invalid"], true);
  assert.equal(input.props["aria-describedby"], "field-help field-error");
  input.props.onChange({ target: { value: "" } });
  assert.equal(change.kind, "set");
  assert.equal(change.value, "");
  const values = controls.exports.settingControlValues({ value: "12" }, { defaultValue: "20" }, change);
  assert.equal(values.inputValue, "");
  assert.equal(values.controlValue, "");
});
