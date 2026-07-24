import assert from "node:assert/strict";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";

const {
  activatePrivateCanvasPersistence,
  canvasSaveBatchMatchesPending,
  clearPrivateCanvasPersistence,
  deleteCanvasEmergencyDraft,
  getCanvasDraft,
  getCanvasEmergencyDraft,
  getCanvasSaveBatch,
  isSuspiciousEmptyCanvasDraft,
  listCanvasDrafts,
  putCanvasDraft,
  putCanvasEmergencyDraft,
  resumePrivateCanvasPersistence,
  SerialCanvasDraftWriter,
} = await import("#canvas-persistence");
const { createDefaultCanvasGraph, createEmptyCanvasGraph } = await import(
  "#canvas-graph"
);
const { createCanvasNode, isCanvasVideoNodeType } = await import(
  "#canvas-registry"
);

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();
  failWrites = false;

  get length() {
    return this.values.size;
  }

  clear() {
    this.values.clear();
  }

  getItem(key: string) {
    return this.values.get(key) ?? null;
  }

  key(index: number) {
    return [...this.values.keys()][index] ?? null;
  }

  removeItem(key: string) {
    this.values.delete(key);
  }

  setItem(key: string, value: string) {
    if (this.failWrites) throw new Error("quota exceeded");
    this.values.set(key, value);
  }
}

function installLocalStorage(storage: Storage): () => void {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: storage,
  });
  return () => {
    if (descriptor) {
      Object.defineProperty(globalThis, "localStorage", descriptor);
    } else {
      delete (globalThis as { localStorage?: Storage }).localStorage;
    }
  };
}

function installIndexedDb({
  drafts = [],
  saveBatch,
}: {
  drafts?: unknown[];
  saveBatch?: unknown;
}): () => void {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "indexedDB");
  const request = (result: unknown) => {
    const value = { result } as IDBRequest;
    queueMicrotask(() => value.onsuccess?.(new Event("success")));
    return value;
  };
  const database = {
    close() {},
    transaction(storeName: string) {
      return {
        objectStore() {
          return {
            get() {
              return request(
                storeName === "drafts" ? drafts[0] : saveBatch,
              );
            },
            index() {
              return {
                getAll() {
                  return request(drafts);
                },
              };
            },
          };
        },
      };
    },
  };
  Object.defineProperty(globalThis, "indexedDB", {
    configurable: true,
    value: {
      open() {
        return request(database);
      },
    },
  });
  return () => {
    if (descriptor) {
      Object.defineProperty(globalThis, "indexedDB", descriptor);
    } else {
      delete (globalThis as { indexedDB?: IDBFactory }).indexedDB;
    }
  };
}

function installClearableIndexedDb() {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "indexedDB");
  const stores = {
    drafts: new Map([["draft", { private: true }]]),
    "save-batches": new Map([["batch", { private: true }]]),
  };
  const request = (result: unknown) => {
    const value = { result } as IDBRequest;
    queueMicrotask(() => value.onsuccess?.(new Event("success")));
    return value;
  };
  const database = {
    close() {},
    transaction(storeNames: string[]) {
      const transaction = {
        objectStore(storeName: keyof typeof stores) {
          return {
            clear() {
              stores[storeName].clear();
            },
          };
        },
      } as unknown as IDBTransaction;
      queueMicrotask(() => transaction.oncomplete?.(new Event("complete")));
      assert.deepEqual(storeNames, ["drafts", "save-batches"]);
      return transaction;
    },
  };
  Object.defineProperty(globalThis, "indexedDB", {
    configurable: true,
    value: {
      open() {
        return request(database);
      },
    },
  });
  return {
    stores,
    restore() {
      if (descriptor) {
        Object.defineProperty(globalThis, "indexedDB", descriptor);
      } else {
        delete (globalThis as { indexedDB?: IDBFactory }).indexedDB;
      }
    },
  };
}

function installDelayedOpenIndexedDb() {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "indexedDB");
  const requests: Array<IDBOpenDBRequest & { result: IDBDatabase }> = [];
  Object.defineProperty(globalThis, "indexedDB", {
    configurable: true,
    value: {
      open() {
        const request = {} as IDBOpenDBRequest & { result: IDBDatabase };
        requests.push(request);
        return request;
      },
    },
  });
  return {
    requests,
    resolve(index: number, database: IDBDatabase) {
      const request = requests[index];
      assert.ok(request);
      request.result = database;
      queueMicrotask(() => request.onsuccess?.(new Event("success")));
    },
    restore() {
      if (descriptor) {
        Object.defineProperty(globalThis, "indexedDB", descriptor);
      } else {
        delete (globalThis as { indexedDB?: IDBFactory }).indexedDB;
      }
    },
  };
}

function emergencyDraft(canvasId = "canvas-1", clientId = "client-1") {
  return {
    canvas_id: canvasId,
    client_id: clientId,
    base_revision: 4,
    graph: createDefaultCanvasGraph(),
    operations: [],
    updated_at: Date.now(),
  };
}

test("empty local canvas drafts cannot replace a non-empty server graph", () => {
  const serverGraph = createDefaultCanvasGraph();
  const emptyDraft = {
    ...serverGraph,
    nodes: [],
    edges: [],
  };

  assert.equal(
    isSuspiciousEmptyCanvasDraft(emptyDraft, serverGraph, []),
    true,
  );
  assert.equal(
    isSuspiciousEmptyCanvasDraft(emptyDraft, serverGraph, [
      {
        op: "remove_nodes",
        operation_schema_version: 1,
        node_ids: serverGraph.nodes.map((node) => node.id),
        edge_ids: serverGraph.edges.map((edge) => edge.id),
      },
    ]),
    false,
  );
  assert.equal(
    isSuspiciousEmptyCanvasDraft(emptyDraft, emptyDraft),
    false,
  );
  assert.equal(
    isSuspiciousEmptyCanvasDraft(serverGraph, serverGraph),
    false,
  );
});

test("draft writes stay serial and rerun with the latest snapshot", async () => {
  let releaseFirst: (() => void) | undefined;
  let snapshot = "first";
  const writes: string[] = [];
  const writer = new SerialCanvasDraftWriter(async () => {
    writes.push(snapshot);
    if (writes.length === 1) {
      await new Promise<void>((resolve) => {
        releaseFirst = resolve;
      });
    }
  });

  const first = writer.request();
  await new Promise<void>((resolve) => setImmediate(resolve));
  snapshot = "latest";
  const second = writer.request();
  assert.equal(first, second);
  releaseFirst?.();
  await second;

  assert.deepEqual(writes, ["first", "latest"]);
});

test("draft writer reports failures and still runs a queued latest write", async () => {
  const failure = new Error("IndexedDB failed");
  const errors: unknown[] = [];
  let attempts = 0;
  let rejectFirst: ((error: Error) => void) | undefined;
  const writer = new SerialCanvasDraftWriter(
    async () => {
      attempts += 1;
      if (attempts === 1) {
        await new Promise<void>((_, reject) => {
          rejectFirst = reject;
        });
      }
    },
    (error) => errors.push(error),
  );

  const first = writer.request();
  await new Promise<void>((resolve) => setImmediate(resolve));
  const second = writer.request();
  rejectFirst?.(failure);
  await second;

  assert.equal(first, second);
  assert.equal(attempts, 2);
  assert.deepEqual(errors, [failure]);
});

test("emergency drafts stay disabled until an identity is accepted", async () => {
  const storage = new MemoryStorage();
  const restore = installLocalStorage(storage);
  try {
    const draft = emergencyDraft();
    await clearPrivateCanvasPersistence();
    assert.equal(putCanvasEmergencyDraft(draft), false);
    await activatePrivateCanvasPersistence("user-a");
    assert.equal(putCanvasEmergencyDraft(draft), true);
    assert.deepEqual(getCanvasEmergencyDraft(draft.canvas_id), draft);
    deleteCanvasEmergencyDraft(draft.canvas_id);
    assert.equal(getCanvasEmergencyDraft(draft.canvas_id), null);
  } finally {
    resumePrivateCanvasPersistence();
    restore();
  }
});

test("private canvas cleanup clears only canvas persistence", async () => {
  const storage = new MemoryStorage();
  const restoreStorage = installLocalStorage(storage);
  const indexedDb = installClearableIndexedDb();
  try {
    storage.setItem("lumen:poster-draft", "keep-me");
    assert.equal(putCanvasEmergencyDraft(emergencyDraft()), true);

    await clearPrivateCanvasPersistence();

    assert.equal(getCanvasEmergencyDraft("canvas-1"), null);
    assert.equal(storage.getItem("lumen:poster-draft"), "keep-me");
    assert.equal(indexedDb.stores.drafts.size, 0);
    assert.equal(indexedDb.stores["save-batches"].size, 0);
    assert.equal(putCanvasEmergencyDraft(emergencyDraft("stale")), false);

    await activatePrivateCanvasPersistence("user-a");
    assert.equal(putCanvasEmergencyDraft(emergencyDraft("user-a")), true);
    await activatePrivateCanvasPersistence("user-a");
    assert.equal(getCanvasEmergencyDraft("user-a")?.canvas_id, "user-a");

    await activatePrivateCanvasPersistence("user-b");
    assert.equal(getCanvasEmergencyDraft("user-a"), null);
    assert.equal(storage.getItem("lumen:poster-draft"), "keep-me");
  } finally {
    resumePrivateCanvasPersistence();
    indexedDb.restore();
    restoreStorage();
  }
});

test("canvas writes started before identity invalidation cannot land afterward", async () => {
  const storage = new MemoryStorage();
  const restoreStorage = installLocalStorage(storage);
  const indexedDb = installDelayedOpenIndexedDb();
  try {
    const pendingWrite = putCanvasDraft(emergencyDraft());
    const cleanup = clearPrivateCanvasPersistence();
    assert.equal(indexedDb.requests.length, 2);

    let oldTransactionOpened = false;
    indexedDb.resolve(0, {
      close() {},
      transaction() {
        oldTransactionOpened = true;
        throw new Error("stale write reached a transaction");
      },
    } as unknown as IDBDatabase);
    await assert.rejects(
      pendingWrite,
      (error: unknown) =>
        error instanceof DOMException && error.name === "AbortError",
    );
    assert.equal(oldTransactionOpened, false);

    const clearTransaction = {
      objectStore() {
        return { clear() {} };
      },
    } as unknown as IDBTransaction;
    indexedDb.resolve(1, {
      close() {},
      transaction() {
        queueMicrotask(() =>
          clearTransaction.oncomplete?.(new Event("complete")),
        );
        return clearTransaction;
      },
    } as unknown as IDBDatabase);
    await cleanup;
  } finally {
    resumePrivateCanvasPersistence();
    indexedDb.restore();
    restoreStorage();
  }
});

test("stale identity activation cannot re-enable persistence after logout", async () => {
  const storage = new MemoryStorage();
  const restoreStorage = installLocalStorage(storage);
  const indexedDb = installDelayedOpenIndexedDb();
  storage.setItem("lumen:canvas-owner:v1", "user-a");
  try {
    const staleActivation = activatePrivateCanvasPersistence("user-b");
    const logoutCleanup = clearPrivateCanvasPersistence();
    assert.equal(indexedDb.requests.length, 2);

    const transaction = {
      objectStore() {
        return { clear() {} };
      },
    } as unknown as IDBTransaction;
    const database = {
      close() {},
      transaction() {
        queueMicrotask(() => transaction.oncomplete?.(new Event("complete")));
        return transaction;
      },
    } as unknown as IDBDatabase;

    indexedDb.resolve(0, database);
    await staleActivation;
    assert.equal(storage.getItem("lumen:canvas-owner:v1"), "user-a");
    assert.equal(putCanvasEmergencyDraft(emergencyDraft("stale")), false);

    indexedDb.resolve(1, database);
    await logoutCleanup;
    assert.equal(putCanvasEmergencyDraft(emergencyDraft("logged-out")), false);
  } finally {
    resumePrivateCanvasPersistence();
    indexedDb.restore();
    restoreStorage();
  }
});

test("emergency recovery accepts legacy unbounded reference ports", () => {
  const storage = new MemoryStorage();
  const restore = installLocalStorage(storage);
  try {
    const graph = createDefaultCanvasGraph();
    for (let index = 0; index < 17; index += 1) {
      const source = createCanvasNode("image_asset", { x: 0, y: index * 40 }, {
        id: `legacy-reference-${index}`,
        config: { image_id: `image-${index}` },
      });
      graph.nodes.push(source);
      graph.edges.push({
        id: `legacy-edge-${index}`,
        source_node_id: source.id,
        source_handle: "image",
        target_node_id: "image-generate-1",
        target_handle: "references",
        data_type: "image",
        binding_mode: "follow_active",
        order: index,
      });
    }
    const draft = {
      ...emergencyDraft("legacy-cardinality"),
      graph,
    };

    assert.equal(putCanvasEmergencyDraft(draft), true);
    assert.deepEqual(
      getCanvasEmergencyDraft("legacy-cardinality"),
      draft,
    );
  } finally {
    restore();
  }
});

test("emergency drafts isolate clients sharing the same canvas", () => {
  const storage = new MemoryStorage();
  const restore = installLocalStorage(storage);
  try {
    const first = {
      ...emergencyDraft("canvas-shared", "client-a"),
      updated_at: 1,
    };
    const second = {
      ...emergencyDraft("canvas-shared", "client-b"),
      updated_at: 2,
    };
    assert.equal(putCanvasEmergencyDraft(first), true);
    assert.equal(putCanvasEmergencyDraft(second), true);

    assert.deepEqual(
      getCanvasEmergencyDraft("canvas-shared", "client-a"),
      first,
    );
    assert.deepEqual(
      getCanvasEmergencyDraft("canvas-shared", "client-b"),
      second,
    );
    assert.deepEqual(getCanvasEmergencyDraft("canvas-shared"), second);

    deleteCanvasEmergencyDraft("canvas-shared", "client-a");
    assert.deepEqual(
      getCanvasEmergencyDraft("canvas-shared", "client-a"),
      second,
    );
    assert.deepEqual(
      getCanvasEmergencyDraft("canvas-shared", "client-b"),
      second,
    );
  } finally {
    restore();
  }
});

test("emergency draft helpers reject corrupt, invalid, and oversized payloads", () => {
  const storage = new MemoryStorage();
  const restore = installLocalStorage(storage);
  try {
    storage.setItem("lumen:canvas-emergency-drafts:v1", "{broken");
    assert.equal(getCanvasEmergencyDraft("canvas-1"), null);
    assert.doesNotThrow(() => deleteCanvasEmergencyDraft("canvas-1"));

    assert.equal(
      putCanvasEmergencyDraft({
        ...emergencyDraft(),
        graph: { nodes: [] } as never,
      }),
      false,
    );
    assert.equal(
      putCanvasEmergencyDraft({
        ...emergencyDraft(),
        operations: [
          {
            op: "update_document_settings",
            operation_schema_version: 1,
            settings: { snap_to_grid: true, grid_size: 16 },
          },
        ],
        operation_group_sizes: [2],
      }),
      false,
    );
    const oversized = emergencyDraft();
    oversized.graph.nodes[0]!.config = { text: "x".repeat(600 * 1024) };
    assert.equal(putCanvasEmergencyDraft(oversized), false);
  } finally {
    restore();
  }
});

test("IndexedDB reads reject corrupt drafts and save batches", async () => {
  const validDraft = {
    ...emergencyDraft(),
    key: "canvas-1:client-1",
  };
  const invalidDraft = structuredClone(validDraft);
  invalidDraft.graph.nodes[0]!.type = "unknown" as never;
  const invalidBatch = {
    key: "canvas-1:client-1",
    canvas_id: "canvas-1",
    client_id: "client-1",
    base_revision: 4,
    mutation_id: "mutation-1",
    operations: [{ op: "old_operation", operation_schema_version: 0 }],
    updated_at: Date.now(),
  };
  const restore = installIndexedDb({
    drafts: [invalidDraft, validDraft],
    saveBatch: invalidBatch,
  });
  try {
    assert.equal(await getCanvasDraft("canvas-1", "client-1"), null);
    assert.deepEqual(await listCanvasDrafts("canvas-1"), [validDraft]);
    assert.equal(await getCanvasSaveBatch("canvas-1", "client-1"), null);
  } finally {
    restore();
  }
});

test("emergency drafts reject unknown nodes, invalid edges, and oversized graphs", () => {
  const storage = new MemoryStorage();
  const restore = installLocalStorage(storage);
  try {
    const unknownNode = emergencyDraft("unknown-node");
    unknownNode.graph.nodes[0]!.type = "unknown" as never;
    storage.setItem(
      "lumen:canvas-emergency-drafts:v1",
      JSON.stringify({ version: 1, drafts: [unknownNode] }),
    );
    assert.equal(getCanvasEmergencyDraft("unknown-node"), null);
    assert.equal(putCanvasEmergencyDraft(unknownNode), false);

    const invalidEdge = emergencyDraft("invalid-edge");
    invalidEdge.graph.edges[0]!.target_handle = "missing";
    assert.equal(putCanvasEmergencyDraft(invalidEdge), false);

    const invalidSize = emergencyDraft("invalid-size");
    invalidSize.graph.nodes[0]!.size = { width: 20_000, height: 180 };
    assert.equal(putCanvasEmergencyDraft(invalidSize), false);

    const invalidPin = emergencyDraft("invalid-pin");
    invalidPin.graph.edges[0]!.binding_mode = "pinned";
    invalidPin.graph.edges[0]!.pinned_execution_id = "execution-1";
    invalidPin.graph.edges[0]!.pinned_output_index = 0;
    assert.equal(putCanvasEmergencyDraft(invalidPin), false);

    const oversizedGraph = emergencyDraft("oversized-graph");
    const template = oversizedGraph.graph.nodes[0]!;
    oversizedGraph.graph.nodes = Array.from(
      { length: 1_001 },
      (_, index) => ({
        ...structuredClone(template),
        id: `node-${index}`,
        position: { x: index, y: 0 },
      }),
    );
    oversizedGraph.graph.edges = [];
    assert.equal(putCanvasEmergencyDraft(oversizedGraph), false);
  } finally {
    restore();
  }
});

test("emergency drafts accept pinned outputs from every executable node", () => {
  const storage = new MemoryStorage();
  const restore = installLocalStorage(storage);
  const executableTypes = [
    "image_generate",
    "image_edit",
    "image_inpaint",
    "image_upscale",
    "video_generate",
    "video_text_generate",
    "video_image_generate",
    "video_reference_generate",
  ] as const;
  try {
    for (const [index, type] of executableTypes.entries()) {
      const source = createCanvasNode(type, { x: 40, y: 40 }, {
        id: `source-${index}`,
      });
      const delivery = createCanvasNode("delivery", { x: 420, y: 40 }, {
        id: `delivery-${index}`,
      });
      const video = isCanvasVideoNodeType(type);
      const graph = {
        ...createEmptyCanvasGraph(),
        nodes: [source, delivery],
        edges: [
          {
            id: `edge-${index}`,
            source_node_id: source.id,
            source_handle: video ? "video" : "image",
            target_node_id: delivery.id,
            target_handle: video ? "videos" : "images",
            data_type: video ? "video" as const : "image" as const,
            binding_mode: "pinned" as const,
            pinned_execution_id: `execution-${index}`,
            pinned_output_index: 0,
            order: 0,
          },
        ],
      };
      assert.equal(
        putCanvasEmergencyDraft({
          ...emergencyDraft(`pinned-${type}`),
          graph,
        }),
        true,
        type,
      );
    }
  } finally {
    restore();
  }
});

test("emergency draft helpers stay bounded and never throw on storage faults", () => {
  const storage = new MemoryStorage();
  const restore = installLocalStorage(storage);
  try {
    for (let index = 0; index < 12; index += 1) {
      assert.equal(
        putCanvasEmergencyDraft({
          ...emergencyDraft(`canvas-${index}`),
          updated_at: index,
        }),
        true,
      );
    }
    assert.equal(getCanvasEmergencyDraft("canvas-0"), null);
    assert.equal(getCanvasEmergencyDraft("canvas-11")?.canvas_id, "canvas-11");

    storage.failWrites = true;
    assert.equal(putCanvasEmergencyDraft(emergencyDraft("quota")), false);
    assert.doesNotThrow(() => deleteCanvasEmergencyDraft("canvas-11"));
  } finally {
    restore();
  }

  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() {
      throw new Error("storage access denied");
    },
  });
  try {
    assert.equal(getCanvasEmergencyDraft("canvas-1"), null);
    assert.equal(putCanvasEmergencyDraft(emergencyDraft()), false);
    assert.doesNotThrow(() => deleteCanvasEmergencyDraft("canvas-1"));
  } finally {
    if (descriptor) {
      Object.defineProperty(globalThis, "localStorage", descriptor);
    } else {
      delete (globalThis as { localStorage?: Storage }).localStorage;
    }
  }
});

test("persisted save batches only replay against the exact pending prefix", () => {
  const operation = {
    op: "update_node_meta" as const,
    operation_schema_version: 1 as const,
    node_id: "prompt-1",
    title: "新标题",
  };
  const batch = {
    base_revision: 4,
    operations: [operation],
  };

  assert.equal(
    canvasSaveBatchMatchesPending(batch, 4, [
      structuredClone(operation),
      {
        op: "remove_edges",
        operation_schema_version: 1,
        edge_ids: ["edge-1"],
      },
    ]),
    true,
  );
  assert.equal(canvasSaveBatchMatchesPending(batch, 5, [operation]), false);
  assert.equal(
    canvasSaveBatchMatchesPending(batch, 4, [
      { ...operation, title: "另一个标题" },
    ]),
    false,
  );
});
