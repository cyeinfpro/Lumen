import { match, ok, strictEqual } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { runInNewContext } from "node:vm";

const panelSource = readFileSync(
  new URL("./RedemptionPanel.tsx", import.meta.url),
  "utf8",
);
const viewsSource = readFileSync(
  new URL("./RedemptionPanelViews.tsx", import.meta.url),
  "utf8",
);
const mobileStylesSource = readFileSync(
  new URL("../admin-mobile.module.css", import.meta.url),
  "utf8",
);
const globalStylesSource = readFileSync(
  new URL("../../globals.css", import.meta.url),
  "utf8",
);

function between(source: string, start: string, end: string): string {
  const startIndex = source.indexOf(start);
  const endIndex = source.indexOf(end, startIndex + start.length);
  ok(startIndex >= 0, `missing start marker: ${start}`);
  ok(endIndex > startIndex, `missing end marker: ${end}`);
  return source.slice(startIndex, endIndex);
}

test("redemption queries expose independent error and retry states", () => {
  const queryWiring = [
    ["codesQ", "<RedemptionCodesCard", "兑换码加载失败"],
    ["walletsQ", "<WalletList", "用户钱包加载失败"],
    ["usageQ", "<RedemptionUsageCard", "兑换记录加载失败"],
    ["txQ", "<WalletDetailSection", "流水加载失败"],
  ] as const;

  for (const [query, component, errorLabel] of queryWiring) {
    const wiring = between(panelSource, component, "/>");
    match(wiring, new RegExp(`${query}\\.isError`));
    match(wiring, new RegExp(`${query}\\.refetch`));
    match(wiring, new RegExp(errorLabel));
  }

  const stateContracts = [
    ["function RedemptionCodesContent", "export function RedemptionCodesCard", "暂无兑换码"],
    ["function RedemptionUsageContent", "export function RedemptionUsageCard", "暂无兑换记录"],
    ["export function WalletList", "function WalletSummaryCard", "没有匹配用户"],
    ["function WalletTransactionsContent", "function WalletTransactionsCard", "暂无流水"],
  ] as const;

  for (const [start, end, emptyLabel] of stateContracts) {
    const stateSource = between(viewsSource, start, end);
    const errorIndex = stateSource.indexOf("isError");
    const retryIndex = stateSource.indexOf("onRetry", errorIndex);
    const emptyIndex = stateSource.indexOf(emptyLabel, errorIndex);
    ok(errorIndex >= 0, `${start} must render an error state`);
    ok(retryIndex > errorIndex, `${start} must expose its retry action`);
    ok(
      emptyIndex > errorIndex,
      `${start} must not present empty before its error state`,
    );
  }

  strictEqual((viewsSource.match(/role="alert"/g) ?? []).length, 4);
});

test("clipboard rejection is consumed and reported to the user", async () => {
  const copyStart = panelSource.indexOf("async function copyText");
  const copyEnd = panelSource.indexOf("function CodesSubpanel", copyStart);
  ok(copyStart >= 0 && copyEnd > copyStart);

  const copySource = panelSource.slice(copyStart, copyEnd);
  match(copySource, /try\s*\{/);
  match(copySource, /catch \(err\)/);
  match(copySource, /toast\.error\("复制失败"/);

  const transpiled = ts.transpileModule(
    `${copySource}\nglobalThis.__copyText = copyText;`,
    {
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.None,
      },
    },
  ).outputText;
  const errors: Array<{ title: string; description?: string }> = [];
  const context = {
    Error,
    navigator: {
      clipboard: {
        writeText: async () => {
          throw new Error("clipboard denied");
        },
      },
    },
    toast: {
      success: () => {
        throw new Error("success toast must not run");
      },
      error: (title: string, options?: { description?: string }) => {
        errors.push({ title, description: options?.description });
      },
    },
  } as Record<string, unknown>;

  runInNewContext(transpiled, context);
  const copyText = context.__copyText as (text: string) => Promise<void>;
  await copyText("secret");
  strictEqual(errors.length, 1);
  strictEqual(errors[0]?.title, "复制失败");
  strictEqual(errors[0]?.description, "clipboard denied");
});

test("batch code modal uses the shared modal lifecycle and constrained layout", () => {
  const modalSource = between(
    viewsSource,
    "export function NewCodesModal",
    "export function WalletSearchForm",
  );

  match(viewsSource, /useBodyScrollLock/);
  match(viewsSource, /useModalLayer/);
  match(modalSource, /useBodyScrollLock\(true\)/);
  match(modalSource, /useModalLayer\(\{/);
  match(modalSource, /open: true/);
  match(modalSource, /rootRef: dialogRef/);
  match(modalSource, /role="dialog"/);
  match(modalSource, /aria-modal="true"/);
  match(modalSource, /aria-labelledby=\{titleId\}/);
  match(modalSource, /aria-describedby=\{descriptionId\}/);
  match(modalSource, /tabIndex=\{-1\}/);
  match(modalSource, /onKeyDown=\{onDialogKeyDown\}/);
  match(modalSource, /data-lumen-modal-layer/);
  match(modalSource, /event\.target === event\.currentTarget/);
  match(
    modalSource,
    /mobile-dialog-panel flex h-\[var\(--mobile-dialog-max-height\)\] min-h-0/,
  );
  match(
    modalSource,
    /mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto overscroll-contain/,
  );
  match(
    modalSource,
    /<footer[\s\S]*mobile-dialog-footer flex shrink-0[\s\S]*px-5 pt-3/,
  );
});

test("admin dialog utilities preserve scroll padding and safe-area footer behavior", () => {
  match(
    mobileStylesSource,
    /\.root :global\(\.mobile-dialog-scroll\)[\s\S]*scroll-padding-bottom/,
  );
  match(
    mobileStylesSource,
    /\.root :global\(\.mobile-dialog-footer\)[\s\S]*min-height: 44px/,
  );
  match(
    globalStylesSource,
    /\.mobile-dialog-footer\s*\{[\s\S]*padding-bottom: var\(--mobile-dialog-footer-pad-bottom\)/,
  );
  match(
    globalStylesSource,
    /\.mobile-dialog-panel\s*\{[\s\S]*min-height: 0/,
  );
});

test("RedemptionPanel modules remain type-correct under the web tsconfig", () => {
  const webRoot = fileURLToPath(new URL("../../../../", import.meta.url));
  const configPath = fileURLToPath(
    new URL("../../../../tsconfig.json", import.meta.url),
  );
  const rootNames = [
    fileURLToPath(new URL("./RedemptionPanel.tsx", import.meta.url)),
    fileURLToPath(new URL("./RedemptionPanelViews.tsx", import.meta.url)),
  ];
  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  strictEqual(config.error, undefined);
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, webRoot);
  strictEqual(parsed.errors.length, 0);
  const program = ts.createProgram({
    rootNames,
    options: { ...parsed.options, incremental: false, noEmit: true },
  });
  const diagnostics = ts
    .getPreEmitDiagnostics(program)
    .filter(
      (diagnostic) =>
        diagnostic.file == null || rootNames.includes(diagnostic.file.fileName),
    );
  strictEqual(
    diagnostics.length,
    0,
    ts.formatDiagnostics(diagnostics, {
      getCanonicalFileName: (fileName) => fileName,
      getCurrentDirectory: () => webRoot,
      getNewLine: () => "\n",
    }),
  );
});
