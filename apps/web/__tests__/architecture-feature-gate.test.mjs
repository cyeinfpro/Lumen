import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { collectArchitectureFindings } from "../scripts/check-architecture.mjs";


function withSourceGraph(files, assertion) {
  const tempRoot = mkdtempSync(
    path.join(os.tmpdir(), "lumen-web-feature-gate-"),
  );
  const srcRoot = path.join(tempRoot, "src");
  try {
    for (const [relativePath, source] of Object.entries(files)) {
      const target = path.join(srcRoot, relativePath);
      mkdirSync(path.dirname(target), { recursive: true });
      writeFileSync(target, source);
    }
    assertion(collectArchitectureFindings({ srcRoot }));
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
}


function rules(findings) {
  return findings.violations.map(({ rule }) => rule);
}


test("cross-feature imports use public entries and cycles show a shortest path", () => {
  withSourceGraph(
    {
      "features/chat/index.ts": 'export { chat } from "./internal/chat";\n',
      "features/chat/internal/chat.ts":
        'import { video } from "@/features/video/internal/video";\n' +
        'export const chat = video;\n',
      "features/video/index.ts":
        'export { video } from "./internal/video";\n',
      "features/video/internal/video.ts":
        'import { chat } from "@/features/chat/internal/chat";\n' +
        'export const video = chat;\n',
    },
    (findings) => {
      assert.deepEqual(
        findings.violations.filter(
          ({ rule }) => rule === "feature-deep-import",
        ),
        [
          {
            rule: "feature-deep-import",
            source: "features/chat/internal/chat.ts",
            target: "features/video/internal/video.ts",
          },
          {
            rule: "feature-deep-import",
            source: "features/video/internal/video.ts",
            target: "features/chat/internal/chat.ts",
          },
        ],
      );
      assert.deepEqual(findings.featureCycles, [
        ["chat", "video", "chat"],
      ]);
    },
  );
});


test("server chains stop at client boundaries and reject unmarked browser globals", () => {
  withSourceGraph(
    {
      "app/page.ts":
        'import { helper } from "@/lib/helper";\n' +
        'import { ClientWidget } from "@/components/ClientWidget";\n' +
        'export const page = [helper, ClientWidget];\n',
      "components/ClientWidget.ts":
        '"use client";\nexport const ClientWidget = window.location.href;\n',
      "lib/browser.ts": "export const width = window.innerWidth;\n",
      "lib/helper.ts":
        'import { width } from "@/lib/browser";\nexport const helper = width;\n',
    },
    (findings) => {
      const browserFindings = findings.violations.filter(
        ({ rule }) => rule === "server-chain-imports-browser-global",
      );
      assert.deepEqual(browserFindings, [
        {
          detail: "window",
          path: ["app/page.ts", "lib/helper.ts", "lib/browser.ts"],
          rule: "server-chain-imports-browser-global",
          source: "app/page.ts",
          target: "lib/browser.ts",
        },
      ]);
    },
  );
});


test("presentational UI cannot own API side effects", () => {
  withSourceGraph(
    {
      "shared/api/account.ts": "export const getMe = () => null;\n",
      "shared/ui/Avatar.tsx":
        'import { getMe } from "@/shared/api/account";\n' +
        'export async function Avatar() { await fetch("/avatar"); return getMe(); }\n',
    },
    (findings) => {
      assert.deepEqual(rules(findings), [
        "presentational-ui-calls-fetch",
        "presentational-ui-imports-api",
      ]);
    },
  );
});


test("stores cannot read peer stores or reverse-depend on UI", () => {
  withSourceGraph(
    {
      "features/chat/store/session.ts":
        'import { jobs } from "@/features/video/store/jobs";\n' +
        'import { ChatView } from "@/features/chat/ui/ChatView";\n' +
        "export const session = [jobs, ChatView];\n",
      "features/chat/ui/ChatView.tsx":
        "export const ChatView = () => null;\n",
      "features/video/store/jobs.ts": "export const jobs = [];\n",
    },
    (findings) => {
      assert.ok(rules(findings).includes("feature-deep-import"));
      assert.ok(rules(findings).includes("store-imports-ui"));
      assert.ok(rules(findings).includes("store-to-store-import"));
    },
  );
});


test("realtime runtime creation belongs to shared realtime", () => {
  withSourceGraph(
    {
      "lib/realtime.ts":
        "export const runtime = new RealtimeRuntime();\n",
      "shared/realtime/runtime.ts":
        "export const runtime = new RealtimeRuntime();\n",
    },
    (findings) => {
      assert.deepEqual(
        findings.violations.filter(
          ({ rule }) => rule === "realtime-runtime-outside-owner",
        ),
        [
          {
            detail: "RealtimeRuntime",
            rule: "realtime-runtime-outside-owner",
            source: "lib/realtime.ts",
            target: "<runtime:RealtimeRuntime>",
          },
        ],
      );
    },
  );
});


test("a compliant feature graph has no architecture findings", () => {
  withSourceGraph(
    {
      "app/page.ts":
        'import { ClientWidget } from "@/components/ClientWidget";\n' +
        "export const page = ClientWidget;\n",
      "components/ClientWidget.ts":
        '"use client";\nexport const ClientWidget = window.location.href;\n',
      "features/chat/index.ts": 'export { chat } from "./service";\n',
      "features/chat/service.ts": "export const chat = true;\n",
      "features/projects/service.ts":
        'import { chat } from "@/features/chat";\n' +
        "export const project = chat;\n",
      "features/projects/ui/ProjectCard.tsx":
        "export const ProjectCard = () => null;\n",
      "shared/api/client.ts": "export const api = true;\n",
      "shared/realtime/runtime.ts":
        "export const runtime = new RealtimeRuntime();\n",
      "shared/ui/Button.tsx": "export const Button = () => null;\n",
    },
    (findings) => {
      assert.deepEqual(findings.violations, []);
      assert.deepEqual(findings.cyclePaths, []);
      assert.deepEqual(findings.featureCycles, []);
    },
  );
});
