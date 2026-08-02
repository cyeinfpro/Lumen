import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import ts from "typescript";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(testDir, "..");

function source(relativePath) {
  return fs.readFileSync(path.join(webRoot, relativePath), "utf8");
}

function looseMock() {
  return new Proxy(
    { __esModule: true },
    {
      get(target, key) {
        if (key === "__esModule") return true;
        if (!(key in target)) {
          target[key] = () => undefined;
        }
        return target[key];
      },
    },
  );
}

function loadModule(relativePath, overrides = {}) {
  const output = ts.transpileModule(source(relativePath), {
    compilerOptions: {
      isolatedModules: true,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: relativePath,
  }).outputText;
  const compiledModule = { exports: {} };
  const requireModule = (id) => {
    if (id in overrides) return overrides[id];
    return looseMock();
  };
  new Function("require", "module", "exports", output)(
    requireModule,
    compiledModule,
    compiledModule.exports,
  );
  return compiledModule.exports;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function composerState(text) {
  return {
    text,
    attachments: [],
    mode: "chat",
    params: {
      aspect_ratio: "1:1",
      count: 2,
    },
    forceIntent: undefined,
    reasoningEffort: "high",
    fast: true,
    webSearch: true,
    fileSearch: false,
    codeInterpreter: false,
    imageGeneration: false,
    mask: null,
  };
}

function cloneComposerState(composer) {
  return {
    ...composer,
    attachments: composer.attachments.map((attachment) => ({
      ...attachment,
    })),
    params: { ...composer.params },
    mask: composer.mask ? { ...composer.mask } : null,
  };
}

function hasSamePreferences(composer, baseline) {
  return (
    composer.mode === baseline.mode &&
    JSON.stringify(composer.params) === JSON.stringify(baseline.params) &&
    composer.reasoningEffort === baseline.reasoningEffort &&
    composer.fast === baseline.fast &&
    composer.webSearch === baseline.webSearch &&
    composer.fileSearch === baseline.fileSearch &&
    composer.codeInterpreter === baseline.codeInterpreter &&
    composer.imageGeneration === baseline.imageGeneration
  );
}

function createPostAbortHarness() {
  let fenceVersion = 8;
  const postStarted = deferred();
  const abortSettled = deferred();
  const backupComposer = composerState("keep this draft");
  const state = {
    currentConvId: "conv-1",
    composerError: null,
    composer: cloneComposerState(backupComposer),
  };
  const get = () => state;
  const set = (update) => {
    const patch = typeof update === "function" ? update(state) : update;
    if (patch && patch !== state) Object.assign(state, patch);
  };
  state.sendMessage = async () => {
    await Promise.resolve();
    set((current) => ({
      composer: {
        ...composerState(""),
        mode: current.composer.mode,
        params: current.composer.params,
        reasoningEffort: current.composer.reasoningEffort,
        fast: current.composer.fast,
        webSearch: current.composer.webSearch,
        fileSearch: current.composer.fileSearch,
        codeInterpreter: current.composer.codeInterpreter,
        imageGeneration: current.composer.imageGeneration,
      },
    }));
    postStarted.resolve();
    await abortSettled.promise;
  };

  const { createGenerationActions } = loadModule(
    "src/store/chat/generationActions.ts",
    {
      "@/lib/api/images": {
        uploadImage: async () => ({ id: "uploaded-mask" }),
      },
      "@/lib/logger": { logWarn() {} },
      "@/lib/utils": { uuid: () => "temp-attachment" },
      "./composerSlice": {
        cloneComposerState,
        inpaintAspectRatio: () => "1:1",
        inpaintValidationError: () => null,
        isResetComposerDraft: (composer, baseline) =>
          composer.text === "" &&
          composer.attachments.length === 0 &&
          composer.mask === null &&
          composer.forceIntent === undefined &&
          hasSamePreferences(composer, baseline),
        isTemporaryInpaintComposerDraft: (
          composer,
          text,
          attachmentId,
          baseline,
        ) =>
          composer.text === text &&
          composer.attachments.length === 1 &&
          composer.attachments[0]?.id === attachmentId &&
          composer.mask?.target_attachment_id === attachmentId &&
          hasSamePreferences(composer, baseline),
      },
      "./runtime": {
        _conversationMutationFence: {
          snapshot: () => fenceVersion,
        },
        isConversationMutationCurrent: (
          currentConvId,
          expectedConvId,
          snapshot,
        ) =>
          currentConvId === expectedConvId && snapshot === fenceVersion,
      },
    },
  );

  return {
    actions: createGenerationActions(set, get, {
      runtimeFastDefault: () => null,
    }),
    backupComposer,
    postStarted,
    state,
    switchConversationAndAbort() {
      fenceVersion += 1;
      state.currentConvId = "conv-2";
      abortSettled.resolve();
    },
  };
}

function inpaintPayload() {
  return {
    sourceImageId: "image-1",
    sourceSrc: "data:image/png;base64,source",
    sourceWidth: 20,
    sourceHeight: 10,
    maskBlob: new Blob(["mask"], { type: "image/png" }),
    maskPreviewDataUrl: "data:image/png;base64,mask",
    prompt: "replace the selected area",
  };
}

test("stale inpaint fence cancels without success UI or draft cleanup", async () => {
  let fenceVersion = 4;
  const uploadStarted = deferred();
  const uploadResult = deferred();
  let uploadCalls = 0;
  let sendCalls = 0;

  const state = {
    currentConvId: "conv-1",
    composerError: null,
    composer: {},
    sendMessage: async () => {
      sendCalls += 1;
    },
  };
  const get = () => state;
  const set = (update) => {
    const patch = typeof update === "function" ? update(state) : update;
    if (patch && patch !== state) Object.assign(state, patch);
  };

  const { createGenerationActions } = loadModule(
    "src/store/chat/generationActions.ts",
    {
      "@/lib/api/images": {
        uploadImage: () => {
          uploadCalls += 1;
          uploadStarted.resolve();
          return uploadResult.promise;
        },
      },
      "@/lib/logger": { logWarn() {} },
      "@/lib/utils": { uuid: () => "temp-attachment" },
      "./composerSlice": {
        inpaintValidationError: () => null,
      },
      "./runtime": {
        _conversationMutationFence: {
          snapshot: () => fenceVersion,
        },
        isConversationMutationCurrent: (
          currentConvId,
          expectedConvId,
          snapshot,
        ) =>
          currentConvId === expectedConvId && snapshot === fenceVersion,
      },
    },
  );
  const generationActions = createGenerationActions(set, get, {
    runtimeFastDefault: () => null,
  });

  const toasts = [];
  const loggedErrors = [];
  const { useInpaintSubmission } = loadModule(
    "src/components/ui/inpaint/useInpaintSubmission.ts",
    {
      react: {
        useCallback: (callback) => callback,
      },
      "@/components/ui/primitives/mobile": {
        pushMobileToast: (...args) => toasts.push(args),
      },
      "@/lib/logger": {
        logError: (...args) => loggedErrors.push(args),
      },
      "@/lib/auth/privateIdentityEpoch": {
        isPrivateIdentitySnapshotCurrent: ({ userId, epoch }) =>
          userId === "user-a" && epoch === 1,
      },
    },
  );

  const submittingRef = { current: false };
  const submittingValues = [];
  const warnings = [];
  const clearedDrafts = [];
  const clearedMaskDrafts = [];
  const results = [];
  let successCalls = 0;
  const submit = useInpaintSubmission({
    ownerUserId: "user-a",
    identityEpoch: 1,
    boardRef: {
      current: {
        exportMask: async () => ({
          coverage: 0.25,
          width: 20,
          height: 10,
          blob: new Blob(["mask"], { type: "image/png" }),
          preview_data_url: "data:image/png;base64,mask",
        }),
      },
    },
    source: {
      imageId: "image-1",
      src: "data:image/png;base64,source",
      width: 20,
      height: 10,
    },
    promptText: "replace the selected area",
    canSubmit: true,
    submittingRef,
    setSubmitting: (value) => submittingValues.push(value),
    setWarning: (value) => warnings.push(value),
    submitInpaintTask: async (payload) => {
      const result = await generationActions.submitInpaintTask(payload);
      results.push(result);
      return result;
    },
    clearDraft: (imageId) => clearedDrafts.push(imageId),
    clearMaskDraft: (imageId) => clearedMaskDrafts.push(imageId),
    onSubmitSuccess: () => {
      successCalls += 1;
    },
  });

  const pendingSubmission = submit();
  await uploadStarted.promise;
  assert.equal(submittingRef.current, true);
  assert.equal(uploadCalls, 1);

  // Simulate switching away and back to the same conversation while upload is
  // pending: the id matches again, but the mutation fence must still cancel.
  fenceVersion += 1;
  uploadResult.resolve({ id: "uploaded-mask" });
  await pendingSubmission;

  assert.deepEqual(results, [{ status: "cancelled" }]);
  assert.equal(sendCalls, 0);
  assert.deepEqual(toasts, []);
  assert.deepEqual(clearedDrafts, []);
  assert.deepEqual(clearedMaskDrafts, []);
  assert.equal(successCalls, 0);
  assert.deepEqual(loggedErrors, []);
  assert.deepEqual(warnings, [null]);
  assert.deepEqual(submittingValues, [true, false]);
  assert.equal(submittingRef.current, false);
});

test("identity switch during mask export cannot submit or write new-user UI", async () => {
  let currentIdentity = { userId: "user-a", epoch: 11 };
  const maskExport = deferred();
  const toasts = [];
  const warnings = [];
  const submittingValues = [];
  const clearedDrafts = [];
  const clearedMaskDrafts = [];
  let submitCalls = 0;
  let successCalls = 0;
  const { useInpaintSubmission } = loadModule(
    "src/components/ui/inpaint/useInpaintSubmission.ts",
    {
      react: {
        useCallback: (callback) => callback,
      },
      "@/components/ui/primitives/mobile": {
        pushMobileToast: (...args) => toasts.push(args),
      },
      "@/lib/logger": { logError() {} },
      "@/lib/auth/privateIdentityEpoch": {
        isPrivateIdentitySnapshotCurrent: ({ userId, epoch }) =>
          userId === currentIdentity.userId &&
          epoch === currentIdentity.epoch,
      },
    },
  );
  const submittingRef = { current: false };
  const submit = useInpaintSubmission({
    ownerUserId: "user-a",
    identityEpoch: 11,
    boardRef: {
      current: {
        exportMask: () => maskExport.promise,
      },
    },
    source: {
      imageId: "image-a",
      src: "data:image/png;base64,a",
    },
    promptText: "replace",
    canSubmit: true,
    submittingRef,
    setSubmitting: (value) => submittingValues.push(value),
    setWarning: (value) => warnings.push(value),
    submitInpaintTask: async () => {
      submitCalls += 1;
      return { status: "submitted" };
    },
    clearDraft: (imageId) => clearedDrafts.push(imageId),
    clearMaskDraft: (imageId) => clearedMaskDrafts.push(imageId),
    onSubmitSuccess: () => {
      successCalls += 1;
    },
  });

  const pending = submit();
  currentIdentity = { userId: "user-b", epoch: 12 };
  maskExport.resolve({
    coverage: 0.2,
    width: 20,
    height: 10,
    blob: new Blob(["mask"], { type: "image/png" }),
    preview_data_url: "data:image/png;base64,mask",
  });
  await pending;

  assert.equal(submitCalls, 0);
  assert.deepEqual(toasts, []);
  assert.deepEqual(clearedDrafts, []);
  assert.deepEqual(clearedMaskDrafts, []);
  assert.equal(successCalls, 0);
  assert.deepEqual(warnings, [null]);
  assert.deepEqual(submittingValues, [true]);
  assert.equal(submittingRef.current, false);
});

test("identity switch during inpaint POST cannot drain old completion UI", async () => {
  let currentIdentity = { userId: "user-a", epoch: 21 };
  const submitStarted = deferred();
  const submitResult = deferred();
  const toasts = [];
  const warnings = [];
  const submittingValues = [];
  const clearedDrafts = [];
  const clearedMaskDrafts = [];
  let successCalls = 0;
  const { useInpaintSubmission } = loadModule(
    "src/components/ui/inpaint/useInpaintSubmission.ts",
    {
      react: {
        useCallback: (callback) => callback,
      },
      "@/components/ui/primitives/mobile": {
        pushMobileToast: (...args) => toasts.push(args),
      },
      "@/lib/logger": { logError() {} },
      "@/lib/auth/privateIdentityEpoch": {
        isPrivateIdentitySnapshotCurrent: ({ userId, epoch }) =>
          userId === currentIdentity.userId &&
          epoch === currentIdentity.epoch,
      },
    },
  );
  const submittingRef = { current: false };
  const submit = useInpaintSubmission({
    ownerUserId: "user-a",
    identityEpoch: 21,
    boardRef: {
      current: {
        exportMask: async () => ({
          coverage: 0.2,
          width: 20,
          height: 10,
          blob: new Blob(["mask"], { type: "image/png" }),
          preview_data_url: "data:image/png;base64,mask",
        }),
      },
    },
    source: {
      imageId: "image-a",
      src: "data:image/png;base64,a",
    },
    promptText: "replace",
    canSubmit: true,
    submittingRef,
    setSubmitting: (value) => submittingValues.push(value),
    setWarning: (value) => warnings.push(value),
    submitInpaintTask: async () => {
      submitStarted.resolve();
      return submitResult.promise;
    },
    clearDraft: (imageId) => clearedDrafts.push(imageId),
    clearMaskDraft: (imageId) => clearedMaskDrafts.push(imageId),
    onSubmitSuccess: () => {
      successCalls += 1;
    },
  });

  const pending = submit();
  await submitStarted.promise;
  currentIdentity = { userId: "user-b", epoch: 22 };
  submitResult.resolve({ status: "submitted" });
  await pending;

  assert.deepEqual(toasts, []);
  assert.deepEqual(clearedDrafts, []);
  assert.deepEqual(clearedMaskDrafts, []);
  assert.equal(successCalls, 0);
  assert.deepEqual(warnings, [null]);
  assert.deepEqual(submittingValues, [true]);
  assert.equal(submittingRef.current, false);
});

test("inpaint abort after POST start restores the operation backup across conversation switch", async () => {
  const harness = createPostAbortHarness();

  const pendingSubmission =
    harness.actions.submitInpaintTask(inpaintPayload());
  await harness.postStarted.promise;
  assert.equal(harness.state.composer.text, "");

  harness.switchConversationAndAbort();
  const result = await pendingSubmission;

  assert.deepEqual(result, { status: "cancelled" });
  assert.deepEqual(harness.state.composer, harness.backupComposer);
});

test("inpaint abort never overwrites a new composer draft written after cancellation", async () => {
  const harness = createPostAbortHarness();

  const pendingSubmission =
    harness.actions.submitInpaintTask(inpaintPayload());
  await harness.postStarted.promise;

  harness.switchConversationAndAbort();
  harness.state.composer = {
    ...harness.state.composer,
    text: "new draft after abort",
  };
  const newComposer = harness.state.composer;
  const result = await pendingSubmission;

  assert.deepEqual(result, { status: "cancelled" });
  assert.equal(harness.state.composer, newComposer);
  assert.equal(harness.state.composer.text, "new draft after abort");
});
