import assert from "node:assert/strict";
import test from "node:test";

import { MemoryTaskStore } from "../src/memory-task-store.js";
import { IdempotencyConflictError } from "../src/task-store.js";

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
