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

function loadBillingQueryKeys(): {
  all: (userId: string) => readonly unknown[];
  wallet: (userId: string) => readonly unknown[];
  walletTransactions: (
    userId: string,
    params: { kind: string; limit: number; pagination: "infinite" },
  ) => readonly unknown[];
  walletTransactionsAll: (userId: string) => readonly unknown[];
  pricing: (userId: string) => readonly unknown[];
  snapshot: (userId: string) => readonly unknown[];
  redemptions: (
    userId: string,
    params: { limit: number; pagination: "infinite" },
  ) => readonly unknown[];
} {
  const identitySource = readFileSync(
    new URL("../../../lib/queries/userScope.ts", import.meta.url),
    "utf8",
  );
  const start = identitySource.indexOf("export const AUTH_USER_QUERY_KEY");
  const end = identitySource.indexOf("export function useUserQueryScope");
  ok(start >= 0 && end > start, "missing query identity helper block");
  const compiled = ts.transpileModule(identitySource.slice(start, end), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const record = { exports: {} as Record<string, unknown> };
  new Script(compiled).runInNewContext({
    module: record,
    exports: record.exports,
  });
  return (record.exports as { userBillingQueryKeys: never })
    .userBillingQueryKeys;
}

function keyPrefixMatches(
  candidate: readonly unknown[],
  prefix: readonly unknown[],
): boolean {
  return (
    candidate.length >= prefix.length &&
    prefix.every((part, index) => part === candidate[index])
  );
}

test("redeeming invalidates only the ledger keys, never the pricing catalogue", () => {
  const keys = loadBillingQueryKeys();
  const modelSource = readFileSync(
    new URL("./useWalletPageModel.ts", import.meta.url),
    "utf8",
  );
  const onSuccess = modelSource
    .slice(
      modelSource.indexOf("onSuccess: async (result)"),
      modelSource.indexOf("onError: (error)"),
    )
    // Strip comments so the rationale prose cannot satisfy a code assertion.
    .replace(/\/\/[^\n]*/g, "");
  ok(onSuccess.length > 0, "missing redeem onSuccess block");

  // The broad `billing` root must be gone: it also covers `billing.pricing`,
  // which the account menu, mobile top bar and composer estimator all mount.
  doesNotMatch(onSuccess, /queryKey: queryKeys\.all/);
  for (const key of [
    "queryKeys.wallet",
    "queryKeys.snapshot",
    "queryKeys.transactionsAll",
    "queryKeys.redemptions",
  ]) {
    ok(onSuccess.includes(key), `redeem must invalidate ${key}`);
  }
  // No client-side balance arithmetic: money is only rendered from a server
  // response, so `redeemCode`'s amount must not be written into the wallet.
  doesNotMatch(onSuccess, /setQueryData/);

  // The narrowed keys still cover every ledger surface by prefix …
  const invalidated = [
    keys.wallet("user-a"),
    keys.snapshot("user-a"),
    keys.walletTransactionsAll("user-a"),
    keys.redemptions("user-a", { limit: 20, pagination: "infinite" }),
  ];
  const filtered = keys.walletTransactions("user-a", {
    kind: "topup_redeem",
    limit: 30,
    pagination: "infinite",
  });
  ok(
    invalidated.some((prefix) => keyPrefixMatches(filtered, prefix)),
    "every transaction filter must be covered by the transactions prefix",
  );
  // … and none of them reach the pricing catalogue.
  const pricing = keys.pricing("user-a");
  ok(
    invalidated.every((prefix) => !keyPrefixMatches(pricing, prefix)),
    "pricing must survive a redemption",
  );
  ok(
    keyPrefixMatches(pricing, keys.all("user-a")),
    "pricing is under the billing root the model no longer invalidates",
  );
});
