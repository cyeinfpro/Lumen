import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import ts from "typescript";

export function loadTsModule(url, overrides = {}, cache = new Map()) {
  const filePath = fileURLToPath(url);
  if (cache.has(filePath)) return cache.get(filePath);
  const source = readFileSync(filePath, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: filePath,
  }).outputText;
  const compiledModule = { exports: {} };
  cache.set(filePath, compiledModule.exports);
  new Function("require", "module", "exports", output)(
    (id) => {
      if (id in overrides) return overrides[id];
      if (!id.startsWith(".")) {
        throw new Error(`missing test dependency: ${id}`);
      }
      const dependencyPath = resolve(dirname(filePath), id);
      const dependencyUrl = pathToFileURL(
        dependencyPath.endsWith(".ts")
          ? dependencyPath
          : `${dependencyPath}.ts`,
      );
      return loadTsModule(dependencyUrl, overrides, cache);
    },
    compiledModule,
    compiledModule.exports,
  );
  cache.set(filePath, compiledModule.exports);
  return compiledModule.exports;
}
