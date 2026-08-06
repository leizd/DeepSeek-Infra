import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { MemoryTaskStore } from "../src/memory-task-store.js";
import { parseSnapshotStream, SnapshotParseError, DEFAULT_JSONL_LIMITS } from "../src/parse-jsonl.js";
import { collectSnapshot, digest, RestoreStateError, type TaskRecord } from "../src/task-store.js";

const arguments_ = {
  target: "tests/test_mcp.py",
  timeoutSeconds: 30,
} as const;

async function* chunksOf(value: string, size = 7): AsyncGenerator<string, void, void> {
  for (let index = 0; index < value.length; index += size) {
    yield value.slice(index, index + size);
  }
}

async function snapshotFrom(tasks: Array<{ idempotencyKey: string }>): Promise<{ snapshot: string; store: MemoryTaskStore }> {
  const store = new MemoryTaskStore();
  for (const [index, task] of tasks.entries()) {
    await store.createOrGet({ idempotencyKey: task.idempotencyKey, arguments: arguments_, now: 10 + index });
  }
  await store.prepareBackup("backup-journal-contract", 20);
  const snapshot = await collectSnapshot(store.exportBackup("backup-journal-contract"));
  return { snapshot, store };
}

function transactionDigest(restoreId: string): string {
  return digest(`transaction:${restoreId}`);
}

test("staged restore prepares without mutating live state and commits through phases", async () => {
  const { snapshot } = await snapshotFrom([{ idempotencyKey: "journal-task-123" }, { idempotencyKey: "journal-task-456" }]);
  const target = new MemoryTaskStore();
  const restoreId = "restore-journal-123";
  const txDigest = transactionDigest(restoreId);

  const prepared = await target.prepareRestore(restoreId, txDigest, chunksOf(snapshot), 30);
  assert.equal(prepared.phase, "prepared");
  assert.equal(prepared.records, 2);
  assert.equal(prepared.sourceDigest, createHash("sha256").update(snapshot, "utf8").digest("hex"));
  assert.equal(target.tasks.size, 0, "prepare must not write live tasks");
  assert.equal(target.idempotency.size, 0, "prepare must not write live idempotency indexes");
  assert.equal(target.restoreEpoch, "initial");

  const retried = await target.prepareRestore(restoreId, txDigest, chunksOf(snapshot), 31);
  assert.equal(retried.preparedDigest, prepared.preparedDigest, "prepare retry is idempotent");

  await assert.rejects(target.commitRestore(restoreId, txDigest, 32), RestoreStateError);
  const intent = await target.commitRestoreIntent(restoreId, txDigest, 33);
  assert.equal(intent.phase, "commit-intent");

  const committed = await target.commitRestore(restoreId, txDigest, 34);
  assert.equal(committed.phase, "committed-pending-complete");
  assert.equal(target.tasks.size, 2);
  for (const task of target.tasks.values()) {
    assert.equal(task.status, "interrupted");
    assert.equal(task.ownerInstance, null);
    assert.equal(task.leaseUntil, null);
    assert.equal(task.restorePending, restoreId);
  }
  const pending = [...target.tasks.keys()];
  assert.equal(await target.get(pending[0] ?? ""), null, "pending tasks are not visible before complete");
  assert.equal(await target.claim("instance-1", 35, 100), null);

  const retriedCommit = await target.commitRestore(restoreId, txDigest, 36);
  assert.equal(retriedCommit.phase, "committed-pending-complete");
  assert.equal(target.tasks.size, 2);

  const completed = await target.completeRestore(restoreId, 37);
  assert.equal(completed.phase, "complete");
  for (const task of target.tasks.values()) {
    assert.equal(task.restorePending, undefined, "complete makes imported tasks visible");
  }
  assert.notEqual(await target.get(pending[0] ?? ""), null);
  assert.equal(target.restoreFence, null);
  assert.equal(target.restoreEpoch, committed.restoreEpoch);

  const retriedComplete = await target.completeRestore(restoreId, 38);
  assert.equal(retriedComplete.phase, "complete");
  await assert.rejects(target.abortRestore(restoreId, 39), RestoreStateError);
});

test("abort from prepared and committed phases rolls back only this transaction", async () => {
  const { snapshot, store: sourceStore } = await snapshotFrom([{ idempotencyKey: "abort-task-123" }]);
  const preExisting = new MemoryTaskStore();
  const kept = await preExisting.createOrGet({ idempotencyKey: "pre-existing-task", arguments: arguments_, now: 5 });

  const restoreId = "restore-abort-123";
  const txDigest = transactionDigest(restoreId);
  const prepared = await preExisting.prepareRestore(restoreId, txDigest, chunksOf(snapshot), 30);
  assert.equal(prepared.phase, "prepared");
  const abortedPrepared = await preExisting.abortRestore(restoreId, 31);
  assert.equal(abortedPrepared.phase, "rolled-back");
  assert.equal(preExisting.tasks.size, 1, "pre-existing task survives abort");
  assert.notEqual(preExisting.tasks.get(kept.task.id), undefined);

  const secondRestore = "restore-abort-456";
  const secondDigest = transactionDigest(secondRestore);
  await preExisting.prepareRestore(secondRestore, secondDigest, chunksOf(snapshot), 32);
  await preExisting.commitRestoreIntent(secondRestore, secondDigest, 33);
  await preExisting.commitRestore(secondRestore, secondDigest, 34);
  const sourceTaskIds = [...sourceStore.tasks.keys()];
  assert.equal(preExisting.tasks.size, 1 + sourceTaskIds.length);

  const aborted = await preExisting.abortRestore(secondRestore, 35);
  assert.equal(aborted.phase, "rolled-back");
  assert.equal(preExisting.tasks.size, 1, "abort deletes only keys inserted by this transaction");
  assert.notEqual(preExisting.tasks.get(kept.task.id), undefined);
  assert.equal(preExisting.idempotency.size, 1);
  assert.equal(preExisting.restoreEpoch, "initial");
  assert.equal(preExisting.restoreFence, null);
  const retriedAbort = await preExisting.abortRestore(secondRestore, 36);
  assert.equal(retriedAbort.phase, "rolled-back");

  const sourceSnapshot = await collectSnapshot(sourceStore.exportBackup("backup-journal-contract"));
  assert.equal(snapshot, sourceSnapshot);
});

test("restore status drives crash recovery and retries converge", async () => {
  const { snapshot } = await snapshotFrom([{ idempotencyKey: "crash-task-123" }]);
  const target = new MemoryTaskStore();
  const restoreId = "restore-crash-123";
  const txDigest = transactionDigest(restoreId);

  assert.equal(await target.restoreStatus(restoreId), null);
  await target.prepareRestore(restoreId, txDigest, chunksOf(snapshot), 30);
  const afterPrepare = await target.restoreStatus(restoreId);
  assert.equal(afterPrepare?.phase, "prepared");

  // Simulate a crash after commit-intent: retry the whole sequence.
  await target.commitRestoreIntent(restoreId, txDigest, 31);
  await target.commitRestoreIntent(restoreId, txDigest, 32);
  await target.commitRestore(restoreId, txDigest, 33);
  const afterCommit = await target.restoreStatus(restoreId);
  assert.equal(afterCommit?.phase, "committed-pending-complete");
  await target.commitRestore(restoreId, txDigest, 34);
  await target.completeRestore(restoreId, 35);
  const finalStatus = await target.restoreStatus(restoreId);
  assert.equal(finalStatus?.phase, "complete");

  const unknown = await target.abortRestore("restore-never-seen", 40);
  assert.equal(unknown.phase, "rolled-back");

  await assert.rejects(
    target.prepareRestore(restoreId, digest("different-transaction"), chunksOf(snapshot), 41),
    RestoreStateError,
  );
});

test("snapshot stream parser enforces the incremental contract", async () => {
  const { snapshot } = await snapshotFrom([{ idempotencyKey: "parser-task-123" }]);
  const lines = snapshot.trimEnd().split("\n");

  const parser = parseSnapshotStream(chunksOf(snapshot, 3));
  let entries = 0;
  for await (const entry of parser.entries) {
    entries += 1;
    assert.notEqual(entry.type, undefined);
  }
  assert.equal(entries, 5);
  assert.equal(parser.digestHex(), createHash("sha256").update(snapshot, "utf8").digest("hex"));

  const duplicateTask = [lines[0], lines[1], lines[1], lines[4]].join("\n") + "\n";
  await assert.rejects(async () => {
    for await (const _ of parseSnapshotStream(chunksOf(duplicateTask)).entries) void _;
  }, SnapshotParseError);

  const afterComplete = snapshot + lines[1] + "\n";
  await assert.rejects(async () => {
    for await (const _ of parseSnapshotStream(chunksOf(afterComplete)).entries) void _;
  }, SnapshotParseError);

  const partialLine = snapshot.trimEnd().slice(0, -10);
  await assert.rejects(async () => {
    for await (const _ of parseSnapshotStream(chunksOf(partialLine)).entries) void _;
  }, SnapshotParseError);

  const mismatchedGeneration = snapshot.replace(/"stateGeneration":(\d+)/u, (_match, group: string) =>
    `"stateGeneration":${String(Number(group) + 1)}`,
  );
  await assert.rejects(async () => {
    for await (const _ of parseSnapshotStream(chunksOf(mismatchedGeneration)).entries) void _;
  }, SnapshotParseError);

  const orphanLog = `${lines[0] ?? ""}\n{"type":"log","schemaVersion":1,"record":{"taskId":"missing","stdout":"","stderr":""}}\n${lines[4] ?? ""}\n`;
  await assert.rejects(async () => {
    for await (const _ of parseSnapshotStream(chunksOf(orphanLog)).entries) void _;
  }, SnapshotParseError);

  const oversized = `${lines[0] ?? ""}\n{"type":"task","schemaVersion":1,"task":{"id":"${"x".repeat(DEFAULT_JSONL_LIMITS.maxLineBytes)}"}}\n`;
  await assert.rejects(async () => {
    for await (const _ of parseSnapshotStream(chunksOf(oversized, 64 * 1024)).entries) void _;
  }, SnapshotParseError);

  const incomplete = lines.slice(0, -1).join("\n") + "\n";
  await assert.rejects(async () => {
    for await (const _ of parseSnapshotStream(chunksOf(incomplete)).entries) void _;
  }, SnapshotParseError);
});

test("restored tasks stay interrupted and never replay after complete", async () => {
  const source = new MemoryTaskStore();
  const created: TaskRecord[] = [];
  for (const [index, key] of ["replay-a", "replay-b"].entries()) {
    const result = await source.createOrGet({ idempotencyKey: key, arguments: arguments_, now: 10 + index });
    created.push(result.task);
  }
  const claimed = await source.claim("instance-1", 12, 100);
  assert.notEqual(claimed, null);
  await source.prepareBackup("backup-replay", 20);
  const snapshot = await collectSnapshot(source.exportBackup("backup-replay"));

  const target = new MemoryTaskStore();
  const restoreId = "restore-replay-123";
  const txDigest = transactionDigest(restoreId);
  await target.prepareRestore(restoreId, txDigest, chunksOf(snapshot), 30);
  await target.commitRestoreIntent(restoreId, txDigest, 31);
  const committed = await target.commitRestore(restoreId, txDigest, 32);
  assert.equal(committed.imported, 2);
  assert.equal(committed.interrupted, 2);
  await target.completeRestore(restoreId, 33);

  for (const task of created) {
    const restored = await target.get(task.id);
    assert.equal(restored?.status, "interrupted");
    assert.equal(restored?.ownerInstance, null);
  }
  assert.equal(await target.claim("instance-2", 40, 100), null, "restored tasks are never re-queued");
});
