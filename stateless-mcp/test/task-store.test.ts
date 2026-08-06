import assert from "node:assert/strict";
import test from "node:test";

import { MemoryTaskStore } from "../src/memory-task-store.js";
import { collectSnapshot } from "../src/task-store.js";
import { digest } from "../src/task-store.js";
import { IdempotencyConflictError } from "../src/task-store.js";
import { BackupFenceError } from "../src/task-store.js";
import { BACKUP_FENCE_TTL_MS } from "../src/task-store.js";
import type { RestoreJournal } from "../src/task-store.js";

async function applySnapshot(
  store: MemoryTaskStore,
  restoreId: string,
  snapshot: string,
  now: number,
): Promise<RestoreJournal> {
  const transactionDigest = digest(`transaction:${restoreId}`);
  const existing = await store.restoreStatus(restoreId);
  if (existing?.phase === "complete") {
    return existing;
  }
  await store.prepareRestore(
    restoreId,
    transactionDigest,
    (async function* () {
      yield snapshot;
    })(),
    now,
  );
  await store.commitRestoreIntent(restoreId, transactionDigest, now + 1);
  await store.commitRestore(restoreId, transactionDigest, now + 2);
  await store.completeRestore(restoreId, now + 3);
  const status = await store.restoreStatus(restoreId);
  if (status === null) {
    throw new Error("restore journal is missing after complete");
  }
  return status;
}

const arguments_ = {
  target: "tests/test_mcp.py",
  timeoutSeconds: 30,
} as const;

test("idempotency key deduplicates an identical test task", async () => {
  const store = new MemoryTaskStore();
  const first = await store.createOrGet({ idempotencyKey: "request-123", arguments: arguments_, now: 10 });
  const retried = await store.createOrGet({ idempotencyKey: "request-123", arguments: arguments_, now: 20 });

  assert.equal(first.deduplicated, false);
  assert.equal(retried.deduplicated, true);
  assert.equal(retried.task.id, first.task.id);
});

test("idempotency key rejects different arguments", async () => {
  const store = new MemoryTaskStore();
  await store.createOrGet({ idempotencyKey: "request-123", arguments: arguments_, now: 10 });

  await assert.rejects(
    store.createOrGet({
      idempotencyKey: "request-123",
      arguments: { ...arguments_, target: "tests/test_web.py" },
      now: 20,
    }),
    IdempotencyConflictError,
  );
});

test("expired worker lease is recovered and stale completion is fenced", async () => {
  const store = new MemoryTaskStore();
  const created = await store.createOrGet({
    idempotencyKey: "request-lease",
    arguments: arguments_,
    now: 10,
  });
  const firstClaim = await store.claim("instance-1", 10, 100);
  assert.equal(firstClaim?.id, created.task.id);
  assert.equal(firstClaim?.attempts, 1);
  assert.equal(await store.claim("instance-2", 109, 100), null);

  const recovered = await store.claim("instance-2", 110, 100);
  assert.equal(recovered?.id, created.task.id);
  assert.equal(recovered?.attempts, 2);
  assert.equal(recovered?.ownerInstance, "instance-2");

  const stale = await store.complete(
    created.task.id,
    "instance-1",
    { stdout: "stale", stderr: "", exitCode: 0, error: null },
    111,
  );
  assert.equal(stale, null);

  const completed = await store.complete(
    created.task.id,
    "instance-2",
    { stdout: "recovered", stderr: "", exitCode: 0, error: null },
    112,
  );
  assert.equal(completed?.status, "succeeded");
  assert.equal(completed?.stdout, "recovered");
});

test("backup fence blocks new work and exports running tasks as interrupted", async () => {
  const store = new MemoryTaskStore();
  const created = await store.createOrGet({ idempotencyKey: "backup-task-123", arguments: arguments_, now: 10 });
  assert.equal((await store.claim("instance-1", 10, 100))?.status, "running");

  await store.prepareBackup("backup-contract-123", 20);
  await assert.rejects(
    store.createOrGet({ idempotencyKey: "blocked-task-123", arguments: arguments_, now: 21 }),
    BackupFenceError,
  );
  assert.equal(await store.claim("instance-2", 21, 100), null);

  const snapshot = await collectSnapshot(store.exportBackup("backup-contract-123"));
  assert.match(snapshot, /"status":"interrupted"/u);
  assert.doesNotMatch(snapshot, /ownerInstance":"instance-1"/u);
  assert.doesNotMatch(snapshot, /leaseUntil":110/u);
  await store.releaseBackup("backup-contract-123");

  const restored = new MemoryTaskStore();
  const result = await applySnapshot(restored, "restore-contract-123", snapshot, 30);
  assert.equal(result.imported, 1);
  assert.equal(result.interrupted, 1);
  const task = await restored.get(created.task.id);
  assert.equal(task?.status, "interrupted");
  assert.equal(task?.ownerInstance, null);
  assert.equal(await restored.claim("instance-3", 40, 100), null);
  const retried = await applySnapshot(restored, "restore-contract-123", snapshot, 99);
  assert.equal(retried.imported, 1, "transaction retry converges on the durable journal");
  assert.equal(retried.restoreEpoch, result.restoreEpoch);
  assert.equal(restored.tasks.size, 1);
});

test("backup fence permits idempotent retries and expires after an abandoned snapshot", async () => {
  const store = new MemoryTaskStore();
  const first = await store.createOrGet({ idempotencyKey: "before-fence", arguments: arguments_, now: 10 });
  await store.prepareBackup("backup-expiry", 20);

  const retried = await store.createOrGet({ idempotencyKey: "before-fence", arguments: arguments_, now: 21 });
  assert.equal(retried.deduplicated, true);
  assert.equal(retried.task.id, first.task.id);
  await assert.rejects(
    store.createOrGet({ idempotencyKey: "during-fence", arguments: arguments_, now: 21 }),
    BackupFenceError,
  );

  const afterExpiry = await store.createOrGet({
    idempotencyKey: "after-fence",
    arguments: arguments_,
    now: 20 + BACKUP_FENCE_TTL_MS,
  });
  assert.equal(afterExpiry.deduplicated, false);
});

test("restore deterministically remaps task and idempotency collisions", async () => {
  const source = new MemoryTaskStore();
  const created = await source.createOrGet({ idempotencyKey: "collision-task-123", arguments: arguments_, now: 10 });
  await source.prepareBackup("backup-collision-123", 20);
  const snapshot = await collectSnapshot(source.exportBackup("backup-collision-123"));

  const target = new MemoryTaskStore();
  target.tasks.set(created.task.id, { ...created.task, requestHash: "different", arguments: { ...arguments_, target: "tests/other.py" } });
  target.idempotency.set(created.task.idempotencyKeyHash, created.task.id);
  const result = await applySnapshot(target, "restore-collision-123", snapshot, 30);
  assert.equal(result.imported, 1);
  assert.notEqual(result.remapped[created.task.id], undefined);
  assert.notEqual(result.remapped[created.task.id], created.task.id);
  assert.equal(target.tasks.size, 2);
  assert.equal(target.idempotency.size, 2);
  const retried = await applySnapshot(target, "restore-collision-123", snapshot, 99);
  assert.equal(retried.imported, 1);
  assert.equal(retried.remapped[created.task.id], result.remapped[created.task.id]);
  assert.equal(retried.restoreEpoch, result.restoreEpoch);
  assert.equal(target.tasks.size, 2);
});
