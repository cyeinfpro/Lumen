import assert from "node:assert/strict";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";
import type {
  TransactionalLockChange,
  TransactionalLockRecord,
  TransactionalLockStore,
} from "./semanticIdempotencyStorageLock";

const {
  createTransactionalLockRequest,
  failClosedLockRequest,
  indexedDbLockRequestOrNull,
} = await import("./semanticIdempotencyStorageLock.ts");
const { webLockRequestOrNull } = await import(
  "./semanticIdempotencyPersistence.ts"
);

class MemoryTransactionalStore implements TransactionalLockStore {
  private readonly records = new Map<string, TransactionalLockRecord>();
  private tail: Promise<void> = Promise.resolve();
  failNextCommit = false;

  transact<T>(
    name: string,
    operation: (record: unknown) => TransactionalLockChange<T>,
    rollback?: () => void,
  ): Promise<T> {
    const run = async () => {
      try {
        const current = this.records.get(name);
        const change = operation(
          current ? structuredClone(current) : undefined,
        );
        if (this.failNextCommit) {
          this.failNextCommit = false;
          throw new Error("injected transaction commit failure");
        }
        if (change.record) {
          this.records.set(name, structuredClone(change.record));
        }
        return change.value;
      } catch (error) {
        rollback?.();
        throw error;
      }
    };
    const queued = this.tail.then(run, run);
    this.tail = queued.then(
      () => undefined,
      () => undefined,
    );
    return queued;
  }

  close(): void {}

  seed(record: TransactionalLockRecord): void {
    this.records.set(record.name, structuredClone(record));
  }

  read(name: string): TransactionalLockRecord | undefined {
    const record = this.records.get(name);
    return record ? structuredClone(record) : undefined;
  }
}

class RootStorage {
  private readonly values = new Map<string, string>();

  get length(): number {
    throw new Error("non-transactional enumeration must not coordinate locks");
  }

  key(): string | null {
    throw new Error("non-transactional enumeration must not coordinate locks");
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

class ManualClock {
  wall = 10_000;
  monotonic = 1_000;

  readonly now = () => this.wall;
  readonly monotonicNow = () => this.monotonic;
  readonly wait = async (delayMs: number) => {
    this.wall += delayMs;
    this.monotonic += delayMs;
    await Promise.resolve();
  };

  advance(delayMs: number): void {
    this.wall += delayMs;
    this.monotonic += delayMs;
  }
}

function tokens(prefix: string): () => string {
  let sequence = 0;
  return () => `${prefix}-${++sequence}`;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function boundedTest(
  name: string,
  callback: () => void | Promise<void>,
): void {
  test(name, { timeout: 3_000 }, callback);
}

async function withDeadline<T>(
  promise: Promise<T>,
  label: string,
  timeoutMs = 1_000,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`${label} exceeded ${timeoutMs}ms`));
    }, timeoutMs);
    promise.then(
      (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function restoreGlobal(
  key: "AbortController" | "navigator",
  descriptor: PropertyDescriptor | undefined,
): void {
  if (descriptor) {
    Object.defineProperty(globalThis, key, descriptor);
  } else {
    Reflect.deleteProperty(globalThis, key);
  }
}

const deterministicTiming = {
  leaseMs: 100,
  heartbeatMs: 20,
  retryMs: 5,
  maxAttempts: 200,
  maxForwardSkewMs: 10,
  reapObservationMs: 40,
  quarantineMs: 100,
} as const;

boundedTest("transactional fallback serializes a delayed dual-context journal race", async () => {
  const store = new MemoryTransactionalStore();
  const root = new RootStorage();
  const firstEntered = deferred<void>();
  const releaseFirst = deferred<void>();
  let active = 0;
  let maxActive = 0;
  const requestA = createTransactionalLockRequest(
    async () => store,
    Date.now,
    tokens("tab-a"),
  );
  const requestB = createTransactionalLockRequest(
    async () => store,
    Date.now,
    tokens("tab-b"),
  );
  const run = (request: typeof requestA, label: string) =>
    request("semantic-journal.lock", { mode: "exclusive" }, async () => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      const prior = JSON.parse(root.getItem("journal") ?? "[]") as string[];
      if (label === "a") {
        firstEntered.resolve(undefined);
        await releaseFirst.promise;
      }
      await Promise.resolve();
      root.setItem("journal", JSON.stringify([...prior, label]));
      active -= 1;
    });

  const first = run(requestA, "a");
  await withDeadline(firstEntered.promise, "first journal contender");
  const second = run(requestB, "b");
  try {
    releaseFirst.resolve(undefined);
    await withDeadline(
      Promise.all([first, second]),
      "dual-context journal race",
    );
  } finally {
    releaseFirst.resolve(undefined);
    await first.catch(() => undefined);
  }

  assert.equal(maxActive, 1);
  assert.deepEqual(JSON.parse(root.getItem("journal") ?? "[]"), ["a", "b"]);
});

for (let probe = 1; probe <= 5; probe += 1) {
  boundedTest(`stale fenced mutation cannot overwrite a replacement ${probe}/5`, async () => {
    const store = new MemoryTransactionalStore();
    const clock = new ManualClock();
    const root = new RootStorage();
    const ownerEntered = deferred<void>();
    const resumeOwner = deferred<void>();
    const requestA = createTransactionalLockRequest(
      async () => store,
      clock.now,
      tokens(`old-${probe}`),
      {
        ...deterministicTiming,
        monotonicNow: clock.monotonicNow,
        wait: clock.wait,
      },
    );
    const requestB = createTransactionalLockRequest(
      async () => store,
      clock.now,
      tokens(`new-${probe}`),
      {
        ...deterministicTiming,
        monotonicNow: clock.monotonicNow,
        wait: clock.wait,
      },
    );
    const stale = requestA(
      `semantic-fence-${probe}.lock`,
      { mode: "exclusive" },
      async (guard) => {
        ownerEntered.resolve(undefined);
        await resumeOwner.promise;
        await guard!.runMutation(() => {
          root.setItem("root", "stale");
        });
      },
    );
    await withDeadline(ownerEntered.promise, "stale owner entry");
    clock.advance(200);
    try {
      await withDeadline(
        requestB(
          `semantic-fence-${probe}.lock`,
          { mode: "exclusive" },
          async (guard) => {
            await guard!.runMutation(() => {
              root.setItem("root", "replacement");
            });
          },
        ),
        "replacement acquisition",
      );
    } finally {
      resumeOwner.resolve(undefined);
    }

    await assert.rejects(
      withDeadline(stale, "stale owner completion"),
      /ownership was lost|clock changed/,
    );
    assert.equal(root.getItem("root"), "replacement");
  });
}

boundedTest("expired owner recovery does not consume a pre-entry chooser lease", async () => {
  const store = new MemoryTransactionalStore();
  const clock = new ManualClock();
  const name = "semantic-continuous-recovery.lock";
  store.seed({
    version: 1,
    name,
    owner: "crashed-owner",
    leaseId: "crashed-lease",
    fence: 7,
    revision: 9,
    updatedAt: clock.wall - 200,
    expiresAt: clock.wall - 100,
  });
  let entered = 0;
  const request = createTransactionalLockRequest(
    async () => store,
    clock.now,
    tokens("recovering"),
    {
      leaseMs: 100,
      heartbeatMs: 20,
      retryMs: 10,
      maxAttempts: 100,
      maxForwardSkewMs: 10,
      reapObservationMs: 120,
      quarantineMs: 120,
      monotonicNow: clock.monotonicNow,
      wait: clock.wait,
    },
  );

  await request(name, { mode: "exclusive" }, () => {
    entered += 1;
  });

  assert.equal(entered, 1);
  assert.equal(store.read(name)?.owner, null);
  assert.equal(store.read(name)?.fence, 8);
  assert.ok(clock.monotonic >= 1_120);
});

boundedTest("live owner contention exhausts bounded attempts without entering", async () => {
  const store = new MemoryTransactionalStore();
  const clock = new ManualClock();
  const name = "semantic-bounded-wait.lock";
  store.seed({
    version: 1,
    name,
    owner: "live-owner",
    leaseId: "live-lease",
    fence: 2,
    revision: 3,
    updatedAt: clock.wall,
    expiresAt: clock.wall + 100,
  });
  let callbackCalls = 0;
  const request = createTransactionalLockRequest(
    async () => store,
    clock.now,
    tokens("bounded-waiter"),
    {
      ...deterministicTiming,
      maxAttempts: 3,
      monotonicNow: clock.monotonicNow,
      wait: clock.wait,
    },
  );

  await assert.rejects(
    request(name, { mode: "exclusive" }, () => {
      callbackCalls += 1;
    }),
    /could not acquire indexeddb storage lock/,
  );
  assert.equal(callbackCalls, 0);
  assert.equal(clock.monotonic, 1_015);
});

boundedTest("concurrent stale-owner observers recover without callback overlap", async () => {
  const store = new MemoryTransactionalStore();
  const clock = new ManualClock();
  const name = "semantic-concurrent-recovery.lock";
  store.seed({
    version: 1,
    name,
    owner: "orphan-owner",
    leaseId: "orphan-lease",
    fence: 3,
    revision: 4,
    updatedAt: clock.wall - 200,
    expiresAt: clock.wall - 100,
  });
  let active = 0;
  let maxActive = 0;
  const entered: string[] = [];
  const run = (label: string) =>
    createTransactionalLockRequest(
      async () => store,
      clock.now,
      tokens(label),
      {
        ...deterministicTiming,
        monotonicNow: clock.monotonicNow,
        wait: clock.wait,
      },
    )(name, { mode: "exclusive" }, async () => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      entered.push(label);
      await Promise.resolve();
      active -= 1;
    });

  await withDeadline(
    Promise.all([run("reaper-a"), run("reaper-b")]),
    "concurrent stale-owner recovery",
  );

  assert.equal(maxActive, 1);
  assert.deepEqual(entered.sort(), ["reaper-a", "reaper-b"]);
  assert.equal(store.read(name)?.owner, null);
  assert.equal(store.read(name)?.fence, 5);
});

boundedTest("heartbeat keeps a live long callback mutually exclusive", async () => {
  const store = new MemoryTransactionalStore();
  let active = 0;
  let maxActive = 0;
  const firstEntered = deferred<void>();
  const releaseFirst = deferred<void>();
  const runtime = {
    leaseMs: 200,
    heartbeatMs: 25,
    retryMs: 2,
    maxAttempts: 1_000,
    maxForwardSkewMs: 50,
    reapObservationMs: 50,
    quarantineMs: 200,
  } as const;
  const run = (
    label: string,
    request = createTransactionalLockRequest(
      async () => store,
      Date.now,
      tokens(label),
      runtime,
    ),
  ) =>
    request("semantic-live-heartbeat.lock", { mode: "exclusive" }, async () => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      if (label === "first") {
        firstEntered.resolve(undefined);
        await releaseFirst.promise;
      }
      active -= 1;
    });

  const first = run("first");
  await withDeadline(firstEntered.promise, "live owner entry");
  const second = run("second");
  try {
    await new Promise((resolve) => setTimeout(resolve, 260));
    assert.equal(maxActive, 1);
  } finally {
    releaseFirst.resolve(undefined);
  }
  await withDeadline(
    Promise.all([first, second]),
    "live owner and waiter completion",
  );
  assert.equal(maxActive, 1);
});

boundedTest("failed transactional commit rolls back a fenced root mutation", async () => {
  const store = new MemoryTransactionalStore();
  const root = new RootStorage();
  root.setItem("root", "prior");
  const request = createTransactionalLockRequest(
    async () => store,
    Date.now,
    tokens("rollback"),
  );

  await assert.rejects(
    request("semantic-rollback.lock", { mode: "exclusive" }, async (guard) => {
      store.failNextCommit = true;
      await guard!.runMutation(
        () => root.setItem("root", "stale"),
        () => {
          if (root.getItem("root") === "stale") {
            root.setItem("root", "prior");
          }
        },
      );
    }),
    /injected transaction commit failure/,
  );
  assert.equal(root.getItem("root"), "prior");
});

boundedTest("unavailable IndexedDB does not create an unsafe fallback", async () => {
  assert.equal(
    indexedDbLockRequestOrNull(null, Date.now, tokens("unavailable")),
    null,
  );
  await assert.rejects(
    failClosedLockRequest()(
      "semantic-unavailable.lock",
      { mode: "exclusive" },
      () => undefined,
    ),
    /reliable cross-context idempotency coordination is unavailable/,
  );
});

boundedTest("IndexedDB SecurityError fails closed before the callback runs", async () => {
  let callbackCalls = 0;
  const error = new Error("IndexedDB access blocked");
  error.name = "SecurityError";
  const request = indexedDbLockRequestOrNull(
    {
      open() {
        throw error;
      },
    } as unknown as IDBFactory,
    Date.now,
    tokens("blocked"),
  );
  assert.ok(request);

  await assert.rejects(
    request(
      "semantic-blocked.lock",
      { mode: "exclusive" },
      () => {
        callbackCalls += 1;
      },
    ),
    /indexeddb storage lock open failed/,
  );
  assert.equal(callbackCalls, 0);
});

boundedTest("Web Lock deadline aborts the queue and gates a late grant", async () => {
  const originalNavigator = Object.getOwnPropertyDescriptor(
    globalThis,
    "navigator",
  );
  let capturedSignal: AbortSignal | null = null;
  let grantLate!: () => Promise<void>;
  let callbackCalls = 0;
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      locks: {
        request<T>(
          _name: string,
          options: { signal: AbortSignal },
          callback: () => T | Promise<T>,
        ): Promise<T> {
          capturedSignal = options.signal;
          const pending = new Promise<T>((_resolve, reject) => {
            options.signal.addEventListener(
              "abort",
              () => reject(options.signal.reason),
              { once: true },
            );
          });
          grantLate = async () => {
            try {
              await callback();
            } catch {
              // The deadline gate must reject the late lock grant.
            }
          };
          return pending;
        },
      },
    },
  });
  try {
    const request = webLockRequestOrNull(20);
    assert.ok(request);
    await assert.rejects(
      request("semantic-web-deadline.lock", { mode: "exclusive" }, () => {
        callbackCalls += 1;
      }),
      /web lock acquisition timed out/,
    );
    assert.equal((capturedSignal as AbortSignal | null)?.aborted, true);
    await grantLate();
    assert.equal(callbackCalls, 0);
  } finally {
    restoreGlobal("navigator", originalNavigator);
  }
});

boundedTest("Web Locks without AbortController are not selected", () => {
  const originalNavigator = Object.getOwnPropertyDescriptor(
    globalThis,
    "navigator",
  );
  const originalController = Object.getOwnPropertyDescriptor(
    globalThis,
    "AbortController",
  );
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { locks: { request() {} } },
  });
  Object.defineProperty(globalThis, "AbortController", {
    configurable: true,
    value: undefined,
  });
  try {
    assert.equal(webLockRequestOrNull(20), null);
  } finally {
    restoreGlobal("AbortController", originalController);
    restoreGlobal("navigator", originalNavigator);
  }
});

boundedTest("Web Locks that complete without a grant fail closed", async () => {
  const originalNavigator = Object.getOwnPropertyDescriptor(
    globalThis,
    "navigator",
  );
  let callbackCalls = 0;
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      locks: {
        request: () => Promise.resolve("not-granted"),
      },
    },
  });
  try {
    const request = webLockRequestOrNull(20);
    assert.ok(request);
    await assert.rejects(
      request("semantic-web-no-grant.lock", { mode: "exclusive" }, () => {
        callbackCalls += 1;
      }),
      /completed without granting the lock/,
    );
    assert.equal(callbackCalls, 0);
  } finally {
    restoreGlobal("navigator", originalNavigator);
  }
});

boundedTest("openStore factory timeout aborts without invoking the callback", async () => {
  let openSignal: AbortSignal | null = null;
  let callbackCalls = 0;
  const request = createTransactionalLockRequest(
    (signal) => {
      openSignal = signal;
      return new Promise<TransactionalLockStore>(() => {});
    },
    Date.now,
    tokens("never-open-store"),
    {
      ...deterministicTiming,
      operationDeadlineMs: 10,
    },
  );

  await assert.rejects(
    request("semantic-never-open.lock", { mode: "exclusive" }, () => {
      callbackCalls += 1;
    }),
    /indexeddb storage lock open timed out/,
  );
  assert.equal((openSignal as AbortSignal | null)?.aborted, true);
  assert.equal(callbackCalls, 0);
});

boundedTest("late openStore success is closed after the caller timed out", async () => {
  const lateStore = deferred<TransactionalLockStore>();
  let closeCalls = 0;
  let callbackCalls = 0;
  const store: TransactionalLockStore = {
    transact: async () => {
      throw new Error("late store must never transact");
    },
    close() {
      closeCalls += 1;
    },
  };
  const request = createTransactionalLockRequest(
    () => lateStore.promise,
    Date.now,
    tokens("late-open-store"),
    {
      ...deterministicTiming,
      operationDeadlineMs: 10,
    },
  );

  await assert.rejects(
    request("semantic-late-open.lock", { mode: "exclusive" }, () => {
      callbackCalls += 1;
    }),
    /indexeddb storage lock open timed out/,
  );
  lateStore.resolve(store);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(closeCalls, 1);
  assert.equal(callbackCalls, 0);
});

boundedTest("IndexedDB open that never dispatches events times out", async () => {
  let callbackCalls = 0;
  const factory = {
    open() {
      return {
        result: undefined,
        error: null,
        transaction: null,
        onupgradeneeded: null,
        onblocked: null,
        onerror: null,
        onsuccess: null,
      } as unknown as IDBOpenDBRequest;
    },
  } as unknown as IDBFactory;
  const request = indexedDbLockRequestOrNull(
    factory,
    Date.now,
    tokens("never-idb-open"),
    {
      ...deterministicTiming,
      operationDeadlineMs: 10,
    },
  );
  assert.ok(request);

  await assert.rejects(
    request("semantic-never-idb-open.lock", { mode: "exclusive" }, () => {
      callbackCalls += 1;
    }),
    /indexeddb storage lock open timed out/,
  );
  assert.equal(callbackCalls, 0);
});

boundedTest("late IndexedDB open success closes the database", async () => {
  let openRequest!: {
    result: IDBDatabase;
    error: DOMException | null;
    transaction: IDBTransaction | null;
    onupgradeneeded: (() => void) | null;
    onblocked: (() => void) | null;
    onerror: (() => void) | null;
    onsuccess: (() => void) | null;
  };
  let closeCalls = 0;
  let callbackCalls = 0;
  const database = {
    objectStoreNames: { contains: () => true },
    close() {
      closeCalls += 1;
    },
    onversionchange: null,
  } as unknown as IDBDatabase;
  const factory = {
    open() {
      openRequest = {
        result: database,
        error: null,
        transaction: null,
        onupgradeneeded: null,
        onblocked: null,
        onerror: null,
        onsuccess: null,
      };
      return openRequest as unknown as IDBOpenDBRequest;
    },
  } as unknown as IDBFactory;
  const request = indexedDbLockRequestOrNull(
    factory,
    Date.now,
    tokens("late-idb-open"),
    {
      ...deterministicTiming,
      operationDeadlineMs: 10,
    },
  );
  assert.ok(request);

  await assert.rejects(
    request("semantic-late-idb-open.lock", { mode: "exclusive" }, () => {
      callbackCalls += 1;
    }),
    /indexeddb storage lock open timed out/,
  );
  openRequest.onsuccess?.();
  assert.equal(closeCalls, 1);
  assert.equal(callbackCalls, 0);
});

boundedTest("hung IndexedDB transaction aborts and ignores late success", async () => {
  let getRequest!: {
    result: unknown;
    error: DOMException | null;
    onsuccess: (() => void) | null;
    onerror: (() => void) | null;
  };
  let abortCalls = 0;
  let closeCalls = 0;
  let putCalls = 0;
  let callbackCalls = 0;
  const transactionState: {
    error: DOMException | null;
    oncomplete: (() => void) | null;
    onerror: (() => void) | null;
    onabort: (() => void) | null;
    abort: () => void;
    objectStore: () => {
      get: () => typeof getRequest;
      put: () => never;
    };
  } = {
    error: null,
    oncomplete: null,
    onerror: null,
    onabort: null,
    abort() {
      abortCalls += 1;
      this.onabort?.();
    },
    objectStore() {
      return {
        get() {
          getRequest = {
            result: undefined,
            error: null,
            onsuccess: null,
            onerror: null,
          };
          return getRequest;
        },
        put() {
          putCalls += 1;
          throw new Error("late transaction must not write");
        },
      };
    },
  };
  const transaction = transactionState as unknown as IDBTransaction;
  const database = {
    objectStoreNames: { contains: () => true },
    transaction: () => transaction,
    close() {
      closeCalls += 1;
    },
    onversionchange: null,
  } as unknown as IDBDatabase;
  const factory = {
    open() {
      const openRequest: {
        result: IDBDatabase;
        error: DOMException | null;
        transaction: IDBTransaction | null;
        onupgradeneeded: (() => void) | null;
        onblocked: (() => void) | null;
        onerror: (() => void) | null;
        onsuccess: (() => void) | null;
      } = {
        result: database,
        error: null,
        transaction: null,
        onupgradeneeded: null,
        onblocked: null,
        onerror: null,
        onsuccess: null,
      };
      queueMicrotask(() => openRequest.onsuccess?.());
      return openRequest as unknown as IDBOpenDBRequest;
    },
  } as unknown as IDBFactory;
  const request = indexedDbLockRequestOrNull(
    factory,
    Date.now,
    tokens("hung-idb-transaction"),
    {
      ...deterministicTiming,
      operationDeadlineMs: 10,
    },
  );
  assert.ok(request);

  await assert.rejects(
    request("semantic-hung-idb.lock", { mode: "exclusive" }, () => {
      callbackCalls += 1;
    }),
    /acquisition transaction timed out/,
  );
  getRequest.onsuccess?.();
  assert.equal(abortCalls, 1);
  assert.equal(putCalls, 0);
  assert.equal(callbackCalls, 0);
  assert.equal(closeCalls, 1);
});

boundedTest("hung heartbeat and release transactions are bounded and cleaned", async () => {
  for (const hangAt of ["heartbeat", "release"] as const) {
    let record: TransactionalLockRecord | undefined;
    let calls = 0;
    let closeCalls = 0;
    let abortedCalls = 0;
    const store: TransactionalLockStore = {
      transact<T>(
        _name: string,
        operation: (stored: unknown) => TransactionalLockChange<T>,
        _rollback?: () => void,
        signal?: AbortSignal,
      ): Promise<T> {
        calls += 1;
        const shouldHang =
          (hangAt === "heartbeat" && calls === 3) ||
          (hangAt === "release" && calls === 4);
        if (shouldHang) {
          signal?.addEventListener(
            "abort",
            () => {
              abortedCalls += 1;
            },
            { once: true },
          );
          return new Promise<T>(() => {});
        }
        const change = operation(record);
        if (change.record) record = structuredClone(change.record);
        return Promise.resolve(change.value);
      },
      close() {
        closeCalls += 1;
      },
    };
    const request = createTransactionalLockRequest(
      async () => store,
      Date.now,
      tokens(`hung-${hangAt}`),
      {
        leaseMs: 100,
        heartbeatMs: 20,
        retryMs: 2,
        maxAttempts: 100,
        maxForwardSkewMs: 10,
        reapObservationMs: 40,
        quarantineMs: 100,
        operationDeadlineMs: 10,
      },
    );

    await assert.rejects(
      request(
        `semantic-hung-${hangAt}.lock`,
        { mode: "exclusive" },
        async () => {
          if (hangAt === "heartbeat") {
            await new Promise((resolve) => setTimeout(resolve, 45));
          }
        },
      ),
      hangAt === "heartbeat"
        ? /lease renewal timed out/
        : /lease release timed out/,
    );
    assert.equal(abortedCalls, 1);
    assert.equal(closeCalls, 1);
  }
});
