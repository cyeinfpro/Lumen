import { deepEqual, doesNotMatch, match, ok } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { Script } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("./page.tsx", import.meta.url), "utf8");

type WalletActivityInput = {
  activity_24h?: {
    topup: { micro: number };
    spend: { micro: number };
  };
};

type WalletActivityReader = (
  wallet: WalletActivityInput | undefined,
) => {
  topup: number;
  spend: number;
};

function loadWalletActivityReader(): WalletActivityReader {
  const sourceFile = ts.createSourceFile(
    "page.tsx",
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const declaration = sourceFile.statements.find(
    (node): node is ts.FunctionDeclaration =>
      ts.isFunctionDeclaration(node) &&
      node.name?.text === "walletActivity24h",
  );
  ok(declaration, "missing walletActivity24h");

  const compiled = ts.transpileModule(`(${declaration.getText(sourceFile)})`, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
    },
  }).outputText;
  return new Script(compiled).runInNewContext() as WalletActivityReader;
}

test("wallet page reads the server activity aggregate instead of paginated transactions", () => {
  const walletActivity24h = loadWalletActivityReader();
  const wallet = {
    activity_24h: {
      topup: { micro: 45_670_000 },
      spend: { micro: 12_340_000 },
    },
    transactions: Array.from({ length: 30 }, () => ({
      amount: { micro: -999_000_000 },
    })),
  };

  deepEqual(
    { ...walletActivity24h(wallet) },
    { topup: 45.67, spend: 12.34 },
  );
  match(source, /const stats24h = walletActivity24h\(wallet\)/);
  doesNotMatch(source, /calculateWalletStats24h|Date\.now\(\)/);
});

test("wallet page renders zero activity while the wallet response is unavailable", () => {
  const walletActivity24h = loadWalletActivityReader();

  deepEqual(
    { ...walletActivity24h(undefined) },
    { topup: 0, spend: 0 },
  );
});

test("wallet page tolerates an old wallet response without activity_24h", () => {
  const walletActivity24h = loadWalletActivityReader();

  deepEqual(
    { ...walletActivity24h({} as WalletActivityInput) },
    { topup: 0, spend: 0 },
  );
});
