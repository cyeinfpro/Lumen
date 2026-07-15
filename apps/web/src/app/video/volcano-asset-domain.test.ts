import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import type {
  VideoAssetOperationOut,
  VideoAssetOut,
} from "../../lib/types";
import type { VolcanoAssetSelectionLike } from "./volcano-asset-domain";
import type { UploadItem } from "./volcano-asset-manager-types";

const domainUrl = new URL("./volcano-asset-domain.ts", import.meta.url);
const domain = (await import(
  domainUrl.href
)) as typeof import("./volcano-asset-domain");
const managerStateUrl = new URL(
  "./volcano-asset-manager-state.ts",
  import.meta.url,
);
const managerState = (await import(
  managerStateUrl.href
)) as typeof import("./volcano-asset-manager-state");

const activeImage: VolcanoAssetSelectionLike = {
  id: "asset-image-1",
  name: "虚拟人像",
  asset_type: "Image" as const,
  status: "Active",
  group_id: "group-1",
};

const activeVideo: VolcanoAssetSelectionLike = {
  id: "asset-video-1",
  name: "虚拟人视频",
  asset_type: "Video" as const,
  status: "Active",
  group_id: "group-1",
};

function operationFixture(
  patch: Partial<VideoAssetOperationOut> = {},
): VideoAssetOperationOut {
  return {
    id: "operation-1",
    action: "create_asset",
    status: "running",
    progress_stage: "submitting",
    attempt: 1,
    delivery_generation: 0,
    retryable: false,
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T00:00:01Z",
    ...patch,
  };
}

function assetFixture(
  id: string,
  assetType: "Image" | "Video",
  patch: Partial<VideoAssetOut> = {},
): VideoAssetOut {
  return {
    id,
    group_id: "group-1",
    name: id,
    asset_type: assetType,
    status: "Active",
    project_name: "project-1",
    create_time: "2026-07-15T00:00:00Z",
    ...patch,
  };
}

function uploadFixture(
  patch: Partial<UploadItem> = {},
): UploadItem {
  return {
    id: "upload-1",
    model: "seedance",
    groupId: "group-1",
    file: null,
    fileName: "person.png",
    fileSize: 128,
    fileLastModified: 1,
    assetType: "Image",
    name: "person",
    phase: "failed",
    imageId: "image-upload-1",
    clientOperationId: "operation-client-1",
    retryMode: "create",
    quotaReserved: false,
    quotaReservationTarget: 1,
    ...patch,
  };
}

test("selection rejects existing assets and enforces per-type remaining limits", () => {
  assert.deepEqual(
    domain.toggleVolcanoAssetSelection({
      current: [],
      candidate: activeImage,
      existingAssetIds: ["asset-image-1"],
      remainingLimits: { image: 1, video: 1 },
    }),
    { items: [], issue: "duplicate" },
  );

  const selectedImage = domain.toggleVolcanoAssetSelection({
    current: [],
    candidate: activeImage,
    existingAssetIds: [],
    remainingLimits: { image: 1, video: 1 },
  });
  assert.deepEqual(selectedImage.items, [activeImage]);

  assert.deepEqual(
    domain.toggleVolcanoAssetSelection({
      current: selectedImage.items,
      candidate: { ...activeImage, id: "asset-image-2" },
      existingAssetIds: [],
      remainingLimits: { image: 1, video: 1 },
    }),
    { items: selectedImage.items, issue: "image_limit" },
  );

  assert.deepEqual(
    domain.toggleVolcanoAssetSelection({
      current: selectedImage.items,
      candidate: activeVideo,
      existingAssetIds: [],
      remainingLimits: { image: 1, video: 1 },
    }).items,
    [activeImage, activeVideo],
  );
});

test("only active assets are selectable and selected assets can be toggled off", () => {
  assert.equal(domain.volcanoAssetStatusKind("Active"), "active");
  assert.equal(domain.volcanoAssetStatusKind("Processing"), "processing");
  assert.equal(domain.volcanoAssetStatusKind("Failed"), "failed");
  assert.equal(domain.volcanoAssetIsSelectable("Processing"), false);

  assert.deepEqual(
    domain.toggleVolcanoAssetSelection({
      current: [],
      candidate: { ...activeVideo, status: "Processing" },
      existingAssetIds: [],
      remainingLimits: { image: 1, video: 1 },
    }),
    { items: [], issue: "unavailable" },
  );
  assert.deepEqual(
    domain.toggleVolcanoAssetSelection({
      current: [activeImage],
      candidate: activeImage,
      existingAssetIds: [],
      remainingLimits: { image: 0, video: 0 },
    }),
    { items: [] },
  );
});

test("ingest validation only applies Lumen size and format safety limits", () => {
  const mib = 1024 * 1024;
  assert.deepEqual(
    domain.validateVolcanoAssetFile({
      name: "portrait.webp",
      type: "image/webp",
      size: 50 * mib,
    }),
    { ok: true, assetType: "Image" },
  );
  assert.deepEqual(
    domain.validateVolcanoAssetFile({
      name: "portrait.png",
      type: "image/png",
      size: 50 * mib + 1,
    }),
    { ok: false, error: "图片不能超过 50 MiB" },
  );
  assert.deepEqual(
    domain.validateVolcanoAssetFile({
      name: "motion.mov",
      type: "video/quicktime",
      size: 64 * mib,
    }),
    { ok: true, assetType: "Video" },
  );
  assert.deepEqual(
    domain.validateVolcanoAssetFile({
      name: "motion.mp4",
      type: "video/mp4",
      size: 64 * mib + 1,
    }),
    { ok: false, error: "视频不能超过 64 MiB" },
  );
  assert.equal(
    domain.validateVolcanoAssetFile(
      {
        name: "short.mp4",
        type: "video/mp4",
        size: 4 * mib,
      },
      0.5,
    ).ok,
    true,
  );
  assert.equal(
    domain.validateVolcanoAssetFile(
      {
        name: "long.mp4",
        type: "video/mp4",
        size: 4 * mib,
      },
      120,
    ).ok,
    true,
  );
  assert.equal(
    domain.validateVolcanoAssetFile({
      name: "animation.gif",
      type: "image/gif",
      size: mib,
    }).ok,
    false,
  );
});

test("project quota usage reports stable used and remaining capacity", () => {
  assert.deepEqual(domain.volcanoQuotaUsage(37, 50), {
    used: 37,
    remaining: 13,
    limit: 50,
    reached: false,
  });
  assert.deepEqual(
    domain.volcanoQuotaUsage(50, domain.VOLCANO_PROJECT_ASSET_LIMIT),
    {
      used: 50,
      remaining: 0,
      limit: 50,
      reached: true,
    },
  );
  assert.deepEqual(
    domain.volcanoQuotaUsage(53, domain.VOLCANO_PROJECT_GROUP_LIMIT),
    {
      used: 53,
      remaining: 0,
      limit: 50,
      reached: true,
    },
  );
  assert.equal(domain.VOLCANO_CREATE_ASSET_QPM, 3);
});

test("quota reservations remain conservative until remote totals catch up", () => {
  const reservations = [
    {
      id: "upload-1",
      quotaReserved: true,
      quotaReservationTarget: 11,
    },
    {
      id: "upload-2",
      quotaReserved: true,
      quotaReservationTarget: 12,
    },
    {
      id: "upload-3",
      quotaReserved: false,
      quotaReservationTarget: 13,
    },
  ];
  assert.equal(domain.volcanoReservedQuotaCount(reservations), 2);
  const partiallySettled = domain.settleVolcanoQuotaReservations(
    reservations,
    11,
  );
  assert.equal(partiallySettled[0].quotaReserved, false);
  assert.equal(partiallySettled[1].quotaReserved, true);
  assert.equal(domain.volcanoReservedQuotaCount(partiallySettled), 1);
  const fullySettled = domain.settleVolcanoQuotaReservations(
    partiallySettled,
    12,
  );
  assert.equal(domain.volcanoReservedQuotaCount(fullySettled), 0);
});

test("operation states, progress copy, and timeout handling are forward compatible", () => {
  assert.equal(domain.volcanoOperationStatusKind("queued"), "pending");
  assert.equal(domain.volcanoOperationStatusKind("running"), "pending");
  assert.equal(domain.volcanoOperationStatusKind("succeeded"), "succeeded");
  assert.equal(domain.volcanoOperationStatusKind("failed"), "failed");
  assert.equal(domain.volcanoOperationStatusKind("future_state"), "unknown");
  assert.equal(
    domain.volcanoOperationStageMessage("normalizing_video"),
    "正在后台转码视频尺寸、帧率与编码",
  );
  assert.equal(
    domain.volcanoOperationTimedOut(
      1_000,
      1_000 + domain.VOLCANO_OPERATION_POLL_TIMEOUT_MS - 1,
    ),
    false,
  );
  assert.equal(
    domain.volcanoOperationTimedOut(
      1_000,
      1_000 + domain.VOLCANO_OPERATION_POLL_TIMEOUT_MS,
    ),
    true,
  );
});

test("operation result union narrows groups, assets, and delete receipts", () => {
  const groupOperation = operationFixture({
    action: "create_group",
    status: "succeeded",
    result: {
      id: "group-1",
      name: "虚拟人",
      title: "虚拟人",
      description: "",
      group_type: "AIGC",
      project_name: "default",
    },
  });
  const assetOperation = operationFixture({
    status: "succeeded",
    result: {
      id: "asset-1",
      group_id: "group-1",
      name: "主持人",
      asset_type: "Image",
      status: "Active",
      project_name: "default",
    },
  });
  const deleteOperation = operationFixture({
    action: "delete_group",
    status: "succeeded",
    result: {
      id: "group-1",
      deleted: true,
      resource_type: "group",
      deleted_asset_ids: ["asset-1", "asset-2"],
    },
  });

  assert.equal(
    domain.volcanoOperationResultKind(groupOperation.result),
    "group",
  );
  assert.equal(
    domain.volcanoOperationGroupResult(groupOperation)?.id,
    "group-1",
  );
  assert.equal(
    domain.volcanoOperationResultKind(assetOperation.result),
    "asset",
  );
  assert.equal(
    domain.volcanoOperationAssetResult(assetOperation)?.id,
    "asset-1",
  );
  assert.equal(
    domain.volcanoOperationResultKind(deleteOperation.result),
    "delete",
  );
  assert.deepEqual(
    domain.volcanoDeletedAssetIds(
      domain.volcanoOperationDeleteResult(deleteOperation),
      ["asset-0", "asset-1"],
    ),
    ["asset-0", "asset-1", "asset-2"],
  );
  assert.equal(
    domain.volcanoOperationIsRetryable(
      operationFixture({
        status: "failed",
        error: {
          code: "temporary",
          message: "retry later",
          retryable: true,
        },
      }),
    ),
    true,
  );
});

test("unknown group creation only accepts one exact group outside the pre-submit baseline", () => {
  const oldGroup = {
    id: "group-old",
    name: "主持人",
    title: "主持人",
    description: "蓝色演播室",
    group_type: "AIGC",
    project_name: "default",
  };
  const newGroup = { ...oldGroup, id: "group-new" };
  const secondNewGroup = { ...oldGroup, id: "group-new-2" };
  const wrongDescription = {
    ...oldGroup,
    id: "group-wrong",
    description: "红色演播室",
  };
  const expected = {
    name: "主持人",
    description: "蓝色演播室",
  };

  assert.equal(
    domain.volcanoUniqueNewGroupMatch([oldGroup], [oldGroup.id], expected),
    null,
  );
  assert.equal(
    domain.volcanoUniqueNewGroupMatch(
      [oldGroup, newGroup, wrongDescription],
      [oldGroup.id],
      expected,
    )?.id,
    newGroup.id,
  );
  assert.equal(
    domain.volcanoUniqueNewGroupMatch(
      [oldGroup, newGroup, secondNewGroup],
      [oldGroup.id],
      expected,
    ),
    null,
  );
  assert.equal(
    domain.volcanoUniqueNewGroupMatch(
      [oldGroup, wrongDescription],
      [oldGroup.id],
      expected,
    ),
    null,
  );
});

test("closing or switching models pauses known operations without resubmitting unknown ones", () => {
  const paused = domain.pauseVolcanoOperationCheckpoints([
    {
      id: "known",
      phase: "pending" as const,
      remoteOperationId: "operation-1",
      recovery: "resume" as const,
    },
    {
      id: "unknown",
      phase: "pending" as const,
      submissionStartedAt: 1_000,
      recovery: "resume" as const,
    },
    {
      id: "not-submitted",
      phase: "pending" as const,
      recovery: "resume" as const,
    },
    {
      id: "done",
      phase: "succeeded" as const,
      recovery: "none" as const,
    },
  ]);

  assert.deepEqual(
    paused.map(({ id, phase, recovery }) => ({ id, phase, recovery })),
    [
      { id: "known", phase: "paused", recovery: "resume" },
      { id: "unknown", phase: "uncertain", recovery: "refresh" },
      { id: "not-submitted", phase: "paused", recovery: "resume" },
      { id: "done", phase: "succeeded", recovery: "none" },
    ],
  );
  assert.equal(domain.volcanoOperationBlocksMutation(paused[0]), true);
  assert.equal(domain.volcanoOperationBlocksMutation(paused[1]), true);
  assert.equal(domain.volcanoOperationBlocksMutation(paused[3]), false);
});

test("hierarchical locks block group and child asset races", () => {
  const groupA = domain.volcanoGroupLockKey("group-a");
  const groupB = domain.volcanoGroupLockKey("group-b");
  const assetA1 = domain.volcanoAssetLockKey("group-a", "asset-1");
  const assetA2 = domain.volcanoAssetLockKey("group-a", "asset-2");
  const assetB1 = domain.volcanoAssetLockKey("group-b", "asset-1");

  assert.equal(domain.volcanoOperationLocksConflict(groupA, assetA1), true);
  assert.equal(domain.volcanoOperationLocksConflict(assetA1, groupA), true);
  assert.equal(domain.volcanoOperationLocksConflict(assetA1, assetA1), true);
  assert.equal(domain.volcanoOperationLocksConflict(assetA1, assetA2), false);
  assert.equal(domain.volcanoOperationLocksConflict(groupA, groupB), false);
  assert.equal(domain.volcanoOperationLocksConflict(groupA, assetB1), false);
  assert.equal(
    domain.volcanoOperationLocksConflict(
      domain.volcanoGroupCreateLockKey(),
      groupA,
    ),
    false,
  );
});

test("create failures distinguish safe retries from unknown submission outcomes", () => {
  assert.equal(
    domain.volcanoCreateFailureRecovery({
      code: "volcano_asset_create_rate_limited",
      status: 429,
    }),
    "retry_create",
  );
  assert.equal(
    domain.volcanoCreateFailureRecovery({
      code: "video_asset_queue_unavailable",
      status: 503,
    }),
    "retry_create",
  );
  assert.equal(
    domain.volcanoCreateFailureRecovery({
      code: "network_error",
      status: 0,
    }),
    "verify",
  );
  assert.equal(
    domain.volcanoCreateFailureRecovery({
      code: "unauthorized",
      status: 401,
    }),
    "none",
  );
  assert.equal(
    domain.volcanoAssetErrorMessage(
      { code: "video_asset_operation_not_found" },
      "fallback",
    ),
    "后台任务记录已过期，请刷新素材库确认结果",
  );
});

test("upload material names use the filename stem and stay within 64 characters", () => {
  assert.equal(
    domain.volcanoAssetNameFromFile("  virtual.person.mov  "),
    "virtual.person",
  );
  assert.equal(domain.volcanoAssetNameFromFile(".png"), "虚拟素材");
  assert.equal(
    domain.volcanoAssetNameFromFile(`${"人".repeat(80)}.png`).length,
    domain.VOLCANO_ASSET_NAME_MAX_LENGTH,
  );
  assert.equal(
    domain.truncateVolcanoAssetName("a".repeat(80)),
    "a".repeat(domain.VOLCANO_ASSET_NAME_MAX_LENGTH),
  );
});

test("asset views invalidate stale queries and preserve server pagination filters", () => {
  const baseView = {
    capabilityReady: true,
    groupId: "group-1",
    search: "person",
    status: "Active" as const,
    type: "all" as const,
    page: 1,
  };
  assert.equal(managerState.assetViewMatches(baseView, baseView), true);
  assert.equal(
    managerState.assetViewMatches(baseView, {
      ...baseView,
      search: "other",
    }),
    false,
  );
  assert.equal(
    managerState.assetViewMatches(baseView, {
      ...baseView,
      status: "Failed",
    }),
    false,
  );
  assert.equal(
    managerState.assetViewMatches(baseView, {
      ...baseView,
      type: "Video",
    }),
    false,
  );
  assert.equal(
    managerState.assetViewMatches(baseView, {
      ...baseView,
      page: 2,
    }),
    false,
  );

  assert.deepEqual(managerState.assetListRequest(baseView, 40), {
    name: "person",
    statuses: ["Active"],
    asset_types: undefined,
    page_number: 1,
    page_size: 40,
  });
  assert.deepEqual(
    managerState.assetListRequest(
      {
        ...baseView,
        search: "",
        status: "all",
        type: "Video",
        page: 2,
      },
      40,
    ),
    {
      name: undefined,
      statuses: undefined,
      asset_types: ["Video"],
      page_number: 2,
      page_size: 40,
    },
  );
});

test("complete scans stop on repeated pages and candidate recovery requires one unique match", () => {
  const firstPage = Array.from({ length: 100 }, (_, index) =>
    assetFixture(`asset-${index}`, "Image"),
  );
  const merged = managerState.mergeUniqueAssetPage(
    firstPage,
    [
      ...firstPage,
      assetFixture("asset-page-2-match", "Image", {
        name: "person",
        create_time: "2026-07-15T00:02:00Z",
      }),
    ],
    3_000,
  );
  assert.equal(merged.added, 1);
  assert.equal(merged.items.length, 101);
  assert.equal(
    managerState.mergeUniqueAssetPage(
      merged.items,
      firstPage,
      3_000,
    ).added,
    0,
  );

  const upload = uploadFixture({
    name: "person",
    submissionStartedAt: Date.parse("2026-07-15T00:01:00Z"),
  });
  assert.deepEqual(
    managerState
      .possibleSubmittedAssets(merged.items, upload)
      .map((item) => item.id),
    ["asset-page-2-match"],
  );
  const ambiguous = [
    ...merged.items,
    assetFixture("asset-page-3-match", "Image", {
      name: "person",
      create_time: "2026-07-15T00:03:00Z",
    }),
  ];
  assert.equal(
    managerState.possibleSubmittedAssets(ambiguous, upload).length,
    2,
  );
});

test("definite CreateAsset failures retire stale operation links before recreation", () => {
  const failedUpload = uploadFixture();
  assert.equal(
    managerState.uploadCreateRetryDecision(failedUpload, {
      phase: "failed",
      recovery: "none",
    }),
    "retire_and_recreate",
  );
  const prepared =
    managerState.prepareUploadForCreateRetry(failedUpload);
  assert.equal(prepared.clientOperationId, undefined);
  assert.equal(prepared.operationId, undefined);
  assert.equal(prepared.submissionStartedAt, undefined);
  assert.equal(prepared.imageId, "image-upload-1");
  assert.equal(prepared.retryMode, "create");

  assert.equal(
    managerState.uploadCreateRetryDecision(
      uploadFixture({
        retryMode: "refresh",
        phase: "needs_refresh",
      }),
      {
        phase: "uncertain",
        recovery: "refresh",
      },
    ),
    "preserve",
  );
  assert.equal(
    managerState.uploadCreateRetryDecision(failedUpload, {
      phase: "uncertain",
      recovery: "refresh",
    }),
    "blocked",
  );
});

test("owned group list total never drives the project group quota", () => {
  const dataSource = readFileSync(
    new URL("./use-volcano-asset-data.ts", import.meta.url),
    "utf8",
  );
  const managerSource = readFileSync(
    new URL("./volcano-asset-manager.tsx", import.meta.url),
    "utf8",
  );
  const viewSource = readFileSync(
    new URL("./volcano-asset-manager-view.tsx", import.meta.url),
    "utf8",
  );

  assert.match(
    dataSource,
    /setGroupTotal\(Math\.max\(result\.total_count, result\.items\.length\)\)/,
  );
  assert.match(
    dataSource,
    /getVideoAssetUsage\(requestedModel,[\s\S]*?setProjectGroupTotal\(remoteGroupTotal\)/,
  );
  assert.match(
    managerSource,
    /const groupQuota =\s*assetData\.projectGroupTotal == null[\s\S]*?assetData\.projectGroupTotal,/,
  );
  assert.doesNotMatch(
    managerSource,
    /const groupQuota =\s*assetData\.groupTotal/,
  );
  assert.match(viewSource, /used=\{quotas\.projectGroupTotal\}/);
  assert.doesNotMatch(viewSource, /used=\{quotas\.groupTotal\}/);
});

test("manager preserves mobile dialog, upload-purpose, and destructive copy contracts", () => {
  const managerModuleNames = [
    "volcano-asset-manager.tsx",
    "volcano-asset-manager-types.ts",
    "volcano-asset-manager-state.ts",
    "volcano-asset-manager-helpers.ts",
    "volcano-asset-manager-components.tsx",
    "volcano-asset-manager-view.tsx",
    "use-volcano-asset-data.ts",
    "use-volcano-operation-controller.ts",
    "use-volcano-upload-controller.ts",
    "use-volcano-upload-polling.ts",
    "use-volcano-upload-queue.ts",
  ];
  const managerModules = managerModuleNames.map((fileName) => ({
    fileName,
    source: readFileSync(new URL(`./${fileName}`, import.meta.url), "utf8"),
  }));
  const managerSourceByName = new Map(
    managerModules.map(({ fileName, source }) => [fileName, source]),
  );
  const managerEntrySource = managerModules[0].source;
  const managerSource = managerModules
    .map(({ source }) => source)
    .join("\n");
  const apiSource = [
    readFileSync(new URL("../../lib/apiClient.ts", import.meta.url), "utf8"),
    readFileSync(
      new URL("../../lib/api/videoAssets.ts", import.meta.url),
      "utf8",
    ),
  ].join("\n");
  const typesSource = [
    readFileSync(new URL("../../lib/types.ts", import.meta.url), "utf8"),
    readFileSync(
      new URL("../../lib/videoAssetTypes.ts", import.meta.url),
      "utf8",
    ),
  ].join("\n");

  for (const { fileName, source } of managerModules) {
    assert.ok(
      source.split("\n").length <= 1500,
      `${fileName} must stay within 1500 lines`,
    );
  }
  assert.match(
    managerEntrySource,
    /export function VolcanoAssetManager\(/,
  );
  assert.match(
    managerEntrySource,
    /export type \{\s*VolcanoAssetManagerProps,\s*VolcanoAssetSelection,\s*\} from "\.\/volcano-asset-manager-types"/,
  );

  assert.match(managerSource, /mobile-dialog-shell/);
  assert.match(managerSource, /mobile-dialog-panel/);
  assert.match(managerSource, /mobile-dialog-scroll/);
  assert.match(managerSource, /mobile-dialog-footer/);
  assert.match(managerSource, /purpose: "volcano_asset"/);
  assert.match(managerSource, /后台优化/);
  assert.match(managerSource, /火山处理中/);
  assert.match(managerSource, /级联删除组内全部云端素材/);
  assert.match(managerSource, /上传后自动优化为火山规格/);
  assert.match(managerSource, /当前火山 Project 共享素材库/);
  assert.match(managerSource, /const processingUploadKey = useMemo\(/);
  assert.match(
    managerSource,
    /timer = window\.setTimeout\(\s*\(\) => void poll\(\),/,
  );
  assert.doesNotMatch(managerSource, /window\.setInterval\(/);
  assert.match(managerSource, /getVideoAssetOperation\(/);
  assert.match(managerSource, /retryVideoAssetOperation\(/);
  assert.match(managerSource, /pauseUploadQueue\(/);
  assert.match(managerSource, /uploadQueuesRef/);
  assert.match(managerSource, /提交请求已发出但结果未知。系统不会自动重发/);
  assert.match(managerSource, /系统不会自动重复创建/);
  assert.match(managerSource, /关闭弹窗会暂停本地上传和状态轮询/);
  assert.match(managerSource, /function AssetPagination\(/);
  assert.match(managerSource, /上一页/);
  assert.match(managerSource, /下一页/);
  assert.doesNotMatch(managerSource, /sort_by: "UpdateTime"/);
  assert.match(managerSource, /等待火山提交配额/);
  assert.match(managerSource, /CreateAsset 3 QPM/);
  assert.match(managerSource, /最终限流由服务端兜底/);
  assert.match(managerSource, /素材总配额/);
  assert.match(managerSource, /素材组总配额/);
  assert.match(
    managerSourceByName.get("use-volcano-asset-data.ts") || "",
    /const refreshProjectAssetTotal[\s\S]*?getVideoAssetUsage\(requestedModel,/,
  );
  assert.match(
    managerSourceByName.get("use-volcano-asset-data.ts") || "",
    /setProjectAssetTotal\(remoteAssetTotal\)[\s\S]*?setProjectGroupTotal\(remoteGroupTotal\)/,
  );
  assert.match(
    managerSource,
    /setGroupTotal\(Math\.max\(result\.total_count, result\.items\.length\)\)/,
  );
  assert.match(
    managerEntrySource,
    /const groupQuota =\s*assetData\.projectGroupTotal == null[\s\S]*?assetData\.projectGroupTotal,/,
  );
  assert.doesNotMatch(
    managerEntrySource,
    /const groupQuota =\s*assetData\.groupTotal/,
  );
  assert.match(
    managerSourceByName.get("volcano-asset-manager-view.tsx") || "",
    /used=\{quotas\.projectGroupTotal\}/,
  );
  assert.doesNotMatch(
    managerSourceByName.get("volcano-asset-manager-view.tsx") || "",
    /used=\{quotas\.groupTotal\}/,
  );
  assert.match(
    managerEntrySource,
    /onSucceeded:[\s\S]*?volcanoOperationGroupResult[\s\S]*?Promise\.all\(\[[\s\S]*?refreshGroups\([\s\S]*?refreshProjectAssetTotal\(true, sessionId\)/,
  );
  assert.match(
    managerEntrySource,
    /verifyUnknown:[\s\S]*?volcanoUniqueNewGroupMatch\([\s\S]*?refreshProjectAssetTotal\(true, sessionId\)[\s\S]*?return Boolean\(matched\)/,
  );
  assert.match(managerSource, /groupCreateDisabledReason/);
  assert.match(managerSource, /uploadDisabledReason/);
  assert.match(managerSource, /function OperationActivity\(/);
  assert.match(managerSource, /phase: VolcanoManagedOperationPhase/);
  assert.match(managerSource, /setDeleteTarget\(null\)/);
  assert.match(managerSource, /正在删除素材组/);
  assert.match(managerSource, /正在删除素材/);
  assert.match(managerSource, /素材名称默认取原文件名去扩展名/);
  assert.match(managerSource, /仅用于\s+Lumen\/火山素材列表展示和搜索/);
  assert.match(managerSource, /安全技术文件名/);
  assert.match(managerSource, /VOLCANO_ASSET_NAME_MAX_LENGTH/);
  assert.match(managerSource, /uploadNamesRef/);
  assert.match(managerSource, /onDeleted: \(assetIds: string\[\]\) => void/);
  assert.match(managerSource, /notifyDeletedReferences\(deletedIds\)/);
  assert.match(managerSource, /notifyDeletedReferences\(deletedGroupAssetIds\)/);
  assert.match(managerSource, /const requestedView = \{ \.\.\.assetViewRef\.current \}/);
  assert.match(
    managerSource,
    /assetViewMatches\(assetViewRef\.current, requestedView\)/,
  );
  assert.match(managerSource, /type: AssetTypeFilter/);
  assert.match(
    managerEntrySource,
    /requestedAssetPage,[\s\S]{0,120}requestedAssetSearch,[\s\S]{0,160}requestedStatusFilter,[\s\S]{0,120}requestedTypeFilter,/,
  );
  assert.match(
    managerSourceByName.get("use-volcano-asset-data.ts") || "",
    /listVideoAssets\(\{\s*model: requestedModel,\s*group_ids: \[requestedGroupId\],\s*\.\.\.assetListRequest\(requestedView, ASSET_PAGE_SIZE\)/,
  );
  assert.match(
    managerSource,
    /asset_types: view\.type === "all" \? undefined : \[view\.type\]/,
  );
  assert.match(managerSource, /setAssetTotal\(result\.total_count\)/);
  assert.match(managerSource, /file: File \| null/);
  assert.match(
    managerSource,
    /updateUpload\(\s*item\.id,\s*\{\s*file: null,\s*imageId\s*\}/,
  );
  assert.match(
    managerSource,
    /updateUpload\(\s*item\.id,\s*\{\s*file: null,\s*videoId\s*\}/,
  );
  assert.match(managerSource, /function uploadNameIsEditable\(/);
  assert.match(managerSource, /function uploadCanBeRemoved\(/);
  assert.match(managerSource, /const blockedUploadIds = useMemo\(/);
  assert.match(
    managerSource,
    /blockedUploadIds=\{uploads\.blockedUploadIds\}/,
  );
  assert.match(
    managerSource,
    /operationBlocked=\{blockedUploadIds\.has\(item\.id\)\}/,
  );
  assert.match(
    managerSource,
    /uploadNameIsEditable\(item\) && !operationBlocked/,
  );
  assert.match(
    managerSource,
    /!uploadCanBeRemoved\(item\) \|\| operationBlocked/,
  );
  assert.match(
    managerSource,
    /managedOperation\s*&&\s*volcanoOperationBlocksMutation\(managedOperation\)/,
  );
  assert.match(managerSource, /VOLCANO_ASSET_SCAN_LIMIT = 3_000/);
  assert.match(
    managerSource,
    /for \(let pageNumber = 1; pageNumber <= maxPages; pageNumber \+= 1\)/,
  );
  assert.match(managerSource, /if \(page\.items\.length === 0\) break/);
  assert.match(managerSource, /merged\.added === 0/);
  assert.match(managerSource, /asset_types: assetTypes/);
  assert.match(
    managerSourceByName.get("use-volcano-upload-controller.ts") || "",
    /verifyUnknown:[\s\S]*?scanVideoAssets\([\s\S]*?possibleSubmittedAssets/,
  );
  assert.match(
    managerSourceByName.get("use-volcano-upload-controller.ts") || "",
    /const verifyUntrackedUpload = useCallback\([\s\S]*?scanVideoAssets\([\s\S]*?possibleSubmittedAssets/,
  );
  assert.match(
    managerSource,
    /uploadCreateRetryDecision\([\s\S]*?retireOperation\([\s\S]*?prepareUploadForCreateRetry\(/,
  );
  assert.match(
    managerSource,
    /clientOperationId: undefined,[\s\S]*?operationId: undefined/,
  );
  assert.match(managerSource, /focus-within:ring-2/);
  assert.match(managerSource, /const submissionMayHaveStarted = Boolean\(/);
  assert.match(
    managerSource,
    /if \(remoteOperationId \|\| submissionMayHaveStarted\)/,
  );
  assert.match(
    managerSource,
    /!volcanoOperationBlocksMutation\(operation\) \? \(/,
  );
  assert.match(apiSource, /purpose\?: "inpaint_mask" \| "volcano_asset"/);
  assert.match(apiSource, /DEFAULT_VIDEO_ASSET_QUOTAS/);
  assert.match(typesSource, /quotas: VideoAssetQuotaLimitsOut/);
  assert.match(typesSource, /delivery_generation: number/);
  for (const functionName of [
    "createVideoAssetGroup",
    "patchVideoAssetGroup",
    "deleteVideoAssetGroup",
    "createVideoAsset",
    "patchVideoAsset",
    "deleteVideoAsset",
  ]) {
    assert.match(
      apiSource,
      new RegExp(
        `export function ${functionName}\\([\\s\\S]*?\\): Promise<VideoAssetOperationOut>`,
      ),
    );
  }
  assert.doesNotMatch(apiSource, /VideoAssetCreateAcceptedOut/);
  assert.match(apiSource, /getVideoAssetOperation/);
  assert.match(apiSource, /retryVideoAssetOperation/);
  for (const action of [
    "create_group",
    "update_group",
    "delete_group",
    "create_asset",
    "update_asset",
    "delete_asset",
  ]) {
    assert.match(managerSource, new RegExp(`"${action}"`));
  }
  assert.match(managerSource, /const enqueueOperation = useCallback\(/);
  assert.match(managerSource, /operationQueuesRef/);
  assert.match(managerSource, /pauseVolcanoOperationCheckpoints/);
  assert.match(managerSource, /remoteOperationId/);
  assert.match(managerSource, /submissionStartedAt/);
  assert.match(managerSource, /recovery: "refresh"/);
  assert.match(managerSource, /runner\.verifyUnknown/);
  assert.match(managerSource, /volcanoOperationIsRetryable\(operation\)/);
  assert.match(managerSource, /retryVideoAssetOperation\(remoteOperationId/);
  assert.match(managerSource, /volcanoOperationGroupResult\(operation\)/);
  assert.match(managerSource, /volcanoOperationAssetResult\(operation\)/);
  assert.match(managerSource, /volcanoOperationDeleteResult\(operation\)/);
  assert.match(
    managerSource,
    /const createGroupBaselineIds = new Set<string>\(\)/,
  );
  assert.match(
    managerSource,
    /prepare:\s*form\.mode === "create"[\s\S]*?createGroupBaselineIds\.add\(group\.id\)/,
  );
  assert.match(
    managerSource,
    /volcanoUniqueNewGroupMatch\(\s*result\.items,\s*createGroupBaselineIds,\s*\{ name, description \}/,
  );
  assert.match(
    managerSource,
    /pauseVolcanoOperationCheckpoints\(\s*operationsRef\.current/,
  );
  assert.match(
    managerSource,
    /item\.phase === "paused" &&\s*item\.recovery === "resume"/,
  );
  assert.match(
    managerSource,
    /target\.kind === "group"[\s\S]*?uploadBlocksGroupMutation\(item\)/,
  );
});
