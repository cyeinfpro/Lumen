import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

// Render the real primitive and its dependencies, not a duplicate class fixture.
export function renderThemeButtons(webRoot) {
  const require = createRequire(resolve(webRoot, "package.json"));
  function load(path) {
    const filename = resolve(webRoot, path);
    const exports = {};
    const compiled = ts.transpileModule(readFileSync(filename, "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
      fileName: filename,
    });
    runInNewContext(compiled.outputText, {
      exports,
      require(name) {
        if (name === "@/lib/utils") return load("src/lib/utils.ts");
        if (name === "./Spinner") return load("src/components/ui/primitives/Spinner.tsx");
        return require(name);
      },
    });
    return exports;
  }
  const { Button } = load("src/components/ui/primitives/Button.tsx");
  return renderToStaticMarkup(createElement("div", null,
    ...["primary", "secondary", "glass", "link"].map((variant) =>
      createElement(Button, { key: variant, variant, id: variant }, variant)),
    createElement(Button, { variant: "link", id: "disabled", disabled: true }, "disabled"),
    createElement(Button, { variant: "primary", id: "loading", loading: true }, "loading"),
  ));
}
