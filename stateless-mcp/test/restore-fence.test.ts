import assert from "node:assert/strict";
import test from "node:test";

import { MemoryTaskStore } from "../src/memory-task-store.js";
import { completeWhenUnfenced } from "../src/test-runner.js";
import { BackupFenceError, digest, RestoreFenceError, type TaskOutcome } from "../src/task-store.js";

const arguments_ = {
  target: "tests/test_mcp.py",
  timeoutSeconds: 30,
} as const;

const outcome: TaskOutcome = { stdout: "ok", stderr: "", exitCode: 0, error: null };

async function* chunksOf(value: string, size = 11): AsyncGenerator<string, void, void> {
  for (let index = 0; index < value.length; index += size) {
    yield value.slice(index, index + size);
  }
}

async function snapshotFromSource(): Promise<string> {
  const source = new MemoryTaskStore();
  await source.createOrGet({ idempotencyKey: "fence-snapshot-task", arguments: arguments_, now: 10 });
  await source.prepareBackup("backup-fence-source", 20);
  return await source.exportBackup("backup-fence-source");
}

async function committedRestore(store: MemoryTaskStore): Promise<string> {
  const snapshot = await snapshotFromSource();
  const restoreId = "restore-fence-123";
  const txDigest = digest(`transaction:${restoreId}`);
  const now = Date.now();
  await store.prepareRestore(restoreId, txDigest, chunksOf(snapshot), now);
  await store.commitRestoreIntent(restoreId, txDigest, now + 1);
  await store.commitRestore(restoreId, txDigest, now + 2);
  return restoreId;
}

test("restore fence blocks durable mutations until complete", async () => {
  const store = new MemoryTaskStore();
  const created = await store.createOrGet({ idempotencyKey: "fence-live-task", arguments: arguments_, now: 10 });
  const claimed = await store.claim("instance-1", 11, 1_000);
  assert.equal(claimed?.id, created.task.id);

  const restoreId = await committedRestore(store);

  await assert.rejects(
    store.createOrGet({ idempotencyKey: "fence-blocked-task", arguments: arguments_, now: 40 }),
    RestoreFenceError,
  );
  assert.equal(await store.claim("instance-2", 41, 1_000), null, "claim stays idle during restore");
  await assert.rejects(
    store.complete(created.task.id, "instance-1", outcome, 42),
    RestoreFenceError,
  );
  assert.equal(
    await store.heartbeat(created.task.id, "instance-1", 43, 1_000),
    true,
    "heartbeat renews leases during restore",
  );
  await assert.rejects(store.prepareBackup("backup-during-restore", 44), RestoreFenceError);

  await store.completeRestore(restoreId, 50);
  const completed = await store.complete(created.task.id, "instance-1", outcome, 51);
  assert.equal(completed?.status, "succeeded");
  const createdAfter = await store.createOrGet({ idempotencyKey: "fence-blocked-task", arguments: arguments_, now: 52 });
  assert.equal(createdAfter.deduplicated, false);
});

test("abort releases the fence and complete retries converge after release", async () => {
  const store = new MemoryTaskStore();
  const restoreId = await committedRestore(store);
  await store.abortRestore(restoreId, 40);
  const created = await store.createOrGet({ idempotencyKey: "fence-after-abort", arguments: arguments_, now: 41 });
  assert.equal(created.deduplicated, false);
});

test("completeWhenUnfenced defers until the restore fence releases", async () => {
  const store = new MemoryTaskStore();
  const created = await store.createOrGet({ idempotencyKey: "defer-task", arguments: arguments_, now: 10 });
  await store.claim("instance-1", 11, 5_000);

  const restoreId = await committedRestore(store);
  const started = Date.now();
  const releaser = setTimeout(() => {
    void store.completeRestore(restoreId, Date.now());
  }, 120);
  try {
    await completeWhenUnfenced(store, created.task.id, "instance-1", outcome, 25, 10_000);
  } finally {
    clearTimeout(releaser);
  }
  const elapsed = Date.now() - started;
  assert.equal(elapsed >= 100, true, "completion was deferred until the fence released");
  assert.equal(store.tasks.get(created.task.id)?.status, "succeeded");
});

test("completeWhenUnfenced gives up after the retry limit", async () => {
  const store = new MemoryTaskStore();
  const created = await store.createOrGet({ idempotencyKey: "defer-limit-task", arguments: arguments_, now: 10 });
  await store.claim("instance-1", 11, 5_000);
  await committedRestore(store);
  await assert.rejects(
    completeWhenUnfenced(store, created.task.id, "instance-1", outcome, 10, 50),
    RestoreFenceError,
  );
});

test("commit refuses while a backup fence is active", async () => {
  const store = new MemoryTaskStore();
  const snapshot = await snapshotFromSource();
  await store.prepareBackup("backup-blocks-restore", 20);
  const restoreId = "restore-during-backup";
  const txDigest = digest(`transaction:${restoreId}`);
  await store.prepareRestore(restoreId, txDigest, chunksOf(snapshot), 30);
  await store.commitRestoreIntent(restoreId, txDigest, 31);
  await assert.rejects(store.commitRestore(restoreId, txDigest, 32), RestoreFenceError);
  await store.abortRestore(restoreId, 33);
  await store.releaseBackup("backup-blocks-restore");
  const created = await store.createOrGet({ idempotencyKey: "post-backup-task", arguments: arguments_, now: 40 });
  assert.equal(created.deduplicated, false);
});

test("backup fence still blocks create while restore fence is inactive", async () => {
  const store = new MemoryTaskStore();
  await store.prepareBackup("backup-only-fence", 20);
  await assert.rejects(
    store.createOrGet({ idempotencyKey: "backup-fenced-task", arguments: arguments_, now: 21 }),
    BackupFenceError,
  );
});
