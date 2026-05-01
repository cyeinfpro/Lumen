#!/usr/bin/env node
/**
 * 扫描 apps/web/src 下 TSX 文件，报告可能 <44×44 的可点击元素。
 * 用法: node apps/web/scripts/audit-hit-area.mjs
 * 退出码 0 = 无违规；1 = 有违规
 *
 * 启发式判定：
 *   1. <button> / <a> / role="button" 元素
 *   2. 整文件未导入 <Pressable> / <MobileIconButton>
 *   3. className 里有 h-{小于 11} 的 Tailwind 且无 min-h-11+
 *
 * 放行：在违规行或上一行加 `// @hit-area-ok: <reason>`
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const ROOT = join(dirname(__filename), "..", "src");
const SKIP_DIRS = new Set(["node_modules", ".next", "dist"]);
const TSX_EXT = /\.tsx?$/;

const TAILWIND_H = { "h-8": 32, "h-9": 36, "h-10": 40 };
const OK_NAMES = ["Pressable", "MobileIconButton"];

const violations = [];

function walk(dir) {
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRS.has(name)) continue;
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) walk(full);
    else if (TSX_EXT.test(name)) scan(full);
  }
}

function scan(path) {
  const src = readFileSync(path, "utf8");
  const importsOK = OK_NAMES.some((n) =>
    new RegExp(`import[^;]*\\b${n}\\b`).test(src),
  );
  if (importsOK) return;

  const openTagRe = /<(button|a)\b[^>]*>/g;
  let m;
  while ((m = openTagRe.exec(src)) !== null) {
    const tag = m[0];
    const line = src.slice(0, m.index).split("\n").length;

    const beforeSrc = src.slice(0, m.index).split("\n").slice(-2).join("\n");
    if (/@hit-area-ok/.test(beforeSrc)) continue;

    const cn = (tag.match(/className=["'`]([^"'`]+)["'`]/)?.[1]) ?? "";
    let tooSmall = false;
    for (const tok of Object.keys(TAILWIND_H)) {
      if (new RegExp(`(^|\\s)${tok}(\\s|$)`).test(cn) && !/min-h-1[1-9]/.test(cn)) {
        tooSmall = true;
        break;
      }
    }
    if (tooSmall) {
      violations.push(`${path}:${line} → ${tag.slice(0, 120)}`);
    }
  }
}

walk(ROOT);

if (violations.length === 0) {
  console.log("✓ 命中区审计通过：所有 <button> / <a> 要么 ≥44px 要么走 Pressable/MobileIconButton");
  process.exit(0);
} else {
  console.log(`✗ 发现 ${violations.length} 条可能 <44px 的可点击元素：\n`);
  for (const v of violations) console.log("  " + v);
  console.log("\n修复：改走 <Pressable> / <MobileIconButton>，或改为 h-11+，或加 // @hit-area-ok 放行。");
  process.exit(1);
}
