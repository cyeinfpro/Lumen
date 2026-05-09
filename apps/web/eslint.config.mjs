import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

// V1 设计语言统一（2026-05-09）：4 条防回归规则。
// ESLint flat config 同名 rule 后块覆盖前块，无法分级 severity。所以 4 条 selector
// 合并到同一 no-restricted-syntax，统一 warn。
// 强调色这条已达成全站 0 违规（5 语义 utility 全覆盖），靠 dev IDE warn 提示防回归即可。
// 如需 CI 硬阻断强调色，单独跑：
//   pnpm exec eslint --no-inline-config --rule '{"no-restricted-syntax":["error",{"selector":"<color-selector>","message":"..."}]}' src/
// 详见 apps/web/DESIGN.md §1/§7/§8。
//
// 注意：本 message 中故意避免裸写 Tailwind utility 名（如 bg-danger / shadow-[var(--shadow-1)]），
// 避免 Tailwind v4 的 content scanner 将 eslint.config.mjs 误识别为 source 文件后产生
// 实际不存在的 utility 警告。需描述时用「全角括号」或自然语言。
const designLanguageRules = {
  files: ["src/**/*.{ts,tsx}"],
  rules: {
    "no-restricted-syntax": [
      "warn",
      {
        // 1) Tailwind 原生强调色：red/emerald/sky/blue/yellow/orange/rose/pink/violet/indigo/cyan/teal/lime/green/fuchsia
        //    保留 amber 作品牌色直引（仅 var 形式）。请改用 5 语义槽 utility（accent / danger / success / warning / info）。
        selector:
          "Literal[value=/\\b(text|bg|border|ring|from|to|via|fill|stroke|outline|decoration|placeholder|caret)-(red|emerald|sky|blue|yellow|orange|rose|pink|fuchsia|violet|indigo|cyan|teal|lime|green)-\\d+\\b/]",
        message:
          "禁用 Tailwind 原生强调色。请改用 5 语义槽 utility（accent/danger/success/warning/info）。详见 apps/web/DESIGN.md §1/§7。例外加 // eslint-disable-next-line。",
      },
      {
        // 2) 圆角 rounded-{xs..3xl}（rounded-full/none 例外）。请改用 var 形式 radius token。
        selector:
          "Literal[value=/\\brounded-(xs|sm|md|lg|xl|2xl|3xl)\\b/]",
        message:
          "禁用 rounded 数值档位。请改用 radius var token（control/card/panel/dialog/sheet/pill）。详见 apps/web/DESIGN.md §2。",
      },
      {
        // 3) inline shadow 数值。请改用 shadow var token。
        selector: "Literal[value=/\\bshadow-\\[0_/]",
        message:
          "禁用 inline shadow 数值。请改用 shadow var token。详见 apps/web/DESIGN.md §3。",
      },
      {
        // 4) text-neutral-* 和 bg-neutral-*。新代码用 fg/bg var token。
        selector:
          "Literal[value=/\\b(text|bg|border|ring|from|to|via|fill|placeholder|caret)-neutral-\\d+\\b/]",
        message:
          "禁用 neutral 色阶。请改用 fg/bg var token（0/1/2/3）。详见 apps/web/DESIGN.md §1.4。",
      },
    ],
  },
};

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  designLanguageRules,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
