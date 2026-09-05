import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

// Use the shipped PostCSS configuration in fresh processes to avoid scanner caches.
export function compileThemeCss(webRoot, cwd = webRoot, stylesheet = resolve(webRoot, "src/app/globals.css")) {
  return execFileSync(process.execPath, ["--input-type=module", "-e", `
    import { readFileSync } from "node:fs";
    import { createRequire } from "node:module";
    import { pathToFileURL } from "node:url";
    const [webRoot, stylesheet] = process.argv.slice(1);
    const require = createRequire(webRoot + "/package.json");
    const postcss = require("postcss");
    const { default: config } = await import(pathToFileURL(webRoot + "/postcss.config.mjs"));
    const plugins = Object.entries(config.plugins).map(([name, options]) =>
      require(name)({ ...options, base: process.cwd() }));
    const result = await postcss(plugins).process(readFileSync(stylesheet, "utf8"), {
      from: stylesheet,
    });
    process.stdout.write(result.css);
  `, webRoot, stylesheet], {
    cwd,
    env: { ...process.env, NODE_ENV: "production" },
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  });
}
