import { deepStrictEqual, match, ok, strictEqual } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import type { AdminUserOut } from "@/lib/types";

const modelUrl = new URL("./users/model.ts", import.meta.url);
const {
  emptyUsersDescription,
  emptyUsersTitle,
  userMatchesFilters,
  userRoleFilterLabel,
} = (await import(modelUrl.href)) as typeof import("./users/model");

const moduleUrls = [
  new URL("../page.tsx", import.meta.url),
  new URL("./UsersPanel.tsx", import.meta.url),
  new URL("./users/UserDialogs.tsx", import.meta.url),
  modelUrl,
];

function adminUser(
  overrides: Partial<AdminUserOut> = {},
): AdminUserOut {
  return {
    id: "user-1",
    email: "Member@Example.com",
    role: "member",
    account_mode: "wallet",
    display_name: "Example Member",
    created_at: "2026-07-30T00:00:00Z",
    generations_count: 1,
    completions_count: 2,
    messages_count: 3,
    ...overrides,
  };
}

test("user filters preserve role, email, and display-name matching", () => {
  const member = adminUser();
  const admin = adminUser({
    id: "admin-1",
    email: "admin@example.com",
    role: "admin",
    display_name: null,
  });

  strictEqual(userMatchesFilters(member, "all", ""), true);
  strictEqual(userMatchesFilters(member, "member", "member@example"), true);
  strictEqual(userMatchesFilters(member, "member", "example member"), true);
  strictEqual(userMatchesFilters(member, "admin", ""), false);
  strictEqual(userMatchesFilters(admin, "admin", "admin"), true);
  strictEqual(userMatchesFilters(admin, "member", "admin"), false);
});

test("user filter and empty-state labels remain unchanged", () => {
  deepStrictEqual(
    (["all", "admin", "member"] as const).map(userRoleFilterLabel),
    ["全部", "管理员", "成员"],
  );
  strictEqual(emptyUsersTitle(0), "暂无用户");
  strictEqual(emptyUsersTitle(1), "没有匹配结果");
  strictEqual(emptyUsersDescription(0), "注册的用户会出现在这里");
  strictEqual(emptyUsersDescription(1), "试试切换角色或换个关键词");
});

test("admin page delegates the user workflow and modules stay within budget", () => {
  const [pageSource, usersSource] = moduleUrls
    .slice(0, 2)
    .map((url) => readFileSync(url, "utf8"));

  match(pageSource, /import \{ UsersPanel \} from "\.\/_panels\/UsersPanel"/);
  match(pageSource, /users: <UsersPanel \/>/);
  ok(!pageSource.includes("useAdminUsersInfiniteQuery"));

  for (const url of moduleUrls) {
    const source = readFileSync(url, "utf8");
    const lineCount = source.trimEnd().split("\n").length;
    ok(lineCount <= 800, `${fileURLToPath(url)} is ${lineCount} lines`);
  }
  match(usersSource, /useAdminUsersInfiniteQuery/);
  match(usersSource, /<UserHistoryDialog/);
  match(usersSource, /<PasswordDialog/);
});

test("admin page and user modules compile under the web TypeScript config", () => {
  const webRoot = fileURLToPath(new URL("../../../../", import.meta.url));
  const configPath = fileURLToPath(
    new URL("../../../../tsconfig.json", import.meta.url),
  );
  const rootNames = moduleUrls.map((url) => fileURLToPath(url));
  const rootNameSet = new Set(rootNames);
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
        diagnostic.file == null || rootNameSet.has(diagnostic.file.fileName),
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
