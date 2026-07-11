import { existsSync } from "node:fs";
import { registerHooks } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const webRoot = new URL("../../../", import.meta.url);

function typescriptFileUrl(url) {
  if (url.protocol !== "file:") return null;
  const path = fileURLToPath(url);
  for (const suffix of [".ts", ".tsx", "/index.ts", "/index.tsx"]) {
    if (existsSync(`${path}${suffix}`)) {
      return pathToFileURL(`${path}${suffix}`).href;
    }
  }
  return null;
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    const candidate = specifier.startsWith("@/")
      ? new URL(`./src/${specifier.slice(2)}`, webRoot).href
      : specifier;
    if (
      candidate.startsWith(".") ||
      candidate.startsWith("/") ||
      candidate.startsWith("file:")
    ) {
      const resolved = new URL(candidate, context.parentURL ?? webRoot);
      const typescriptUrl = typescriptFileUrl(resolved);
      if (typescriptUrl) return { url: typescriptUrl, shortCircuit: true };
    }
    return nextResolve(candidate, context);
  },
});
