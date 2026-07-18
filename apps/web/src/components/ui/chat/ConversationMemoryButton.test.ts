import { doesNotMatch, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const modelSource = readFileSync(
  new URL("./ConversationMemoryButton.tsx", import.meta.url),
  "utf8",
);
const viewSource = readFileSync(
  new URL("./ConversationMemoryButtonView.tsx", import.meta.url),
  "utf8",
);

test("conversation memory data stays scoped to the active user and conversation", () => {
  match(
    modelSource,
    /const canQueryConversation =\s*userScope\.enabled && Boolean\(currentConvId\)/,
  );
  match(
    modelSource,
    /queryKey:\s*userConversationQueryKeys\.detail\(\s*userScope\.userId/,
  );
  match(
    modelSource,
    /queryKey:\s*userConversationQueryKeys\.usedMemories\(\s*userScope\.userId/,
  );
  match(
    modelSource,
    /queryKey:\s*userMemoryQueryKeys\.scopes\(userScope\.userId\)/,
  );
  match(modelSource, /enabled: canQueryConversation/);
  match(modelSource, /enabled: open && userScope\.enabled/);
  match(modelSource, /enabled: open && canQueryConversation/);
  match(modelSource, /if \(!canQueryConversation\) return/);
  match(
    modelSource,
    /userConversationQueryKeys\.detail\(\s*userScope\.userId,\s*conversationId/,
  );
  match(
    modelSource,
    /userConversationQueryKeys\.usedMemories\(\s*userScope\.userId,\s*conversationId/,
  );
  doesNotMatch(
    modelSource,
    /queryKey:\s*\["(?:conversation|me",\s*"memory)/,
  );
});

test("conversation memory view keeps controls gated and renders recent memory summaries", () => {
  match(viewSource, /disabled=\{!canQueryConversation\}/);
  match(
    viewSource,
    /disabled=\{togglePending \|\| !canQueryConversation\}/,
  );
  match(
    viewSource,
    /disabled=\{scopePending \|\| scopes\.length === 0 \|\| !canQueryConversation\}/,
  );
  match(viewSource, /\.filter\(\(scope\) => !scope\.is_default\)/);
  match(viewSource, /used\.slice\(0, 6\)\.map/);
  match(viewSource, /\{memory\.type\}/);
  match(viewSource, /\{memory\.content\}/);
  match(viewSource, /href="\/settings\/memory"/);
  match(viewSource, /onClick=\{onClose\}/);
});
