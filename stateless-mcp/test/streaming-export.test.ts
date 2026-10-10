import assert from "node:assert/strict";
import test from "node:test";

import { MemoryTaskStore } from "../src/memory-task-store.js";
import { collectSnapshot } from "../src/task-store.js";

const arguments_ = {
  target: "tests/test_mcp.py",
  timeoutSeconds: 30,
} as const;

test("backup export streams line by line without a monolithic snapshot", async () => {
  const store = new MemoryTaskStore();
  for (let index = 0; index < 500; index += 1) {
    await store.createOrGet({ idempotencyKey: `stream-task-${String(index)}`, arguments: arguments_, now: 10 + index });
  }
  await store.prepareBackup("backup-stream-123", 20);

  const iterable = store.exportBackup("backup-stream-123");
  assert.equal(typeof iterable[Symbol.asyncIterator], "function", "exportBackup returns an async iterable");

  const iterator = iterable[Symbol.asyncIterator]();
  const first = await iterator.next();
  assert.equal(first.done, false);
  const metadata = JSON.parse(first.value) as { type?: string; stateGeneration?: number };
  assert.equal(metadata.type, "metadata");
  assert.equal(metadata.stateGeneration, 500);
  await iterator.return?.();

  let lines = 0;
  for await (const chunk of store.exportBackup("backup-stream-123")) {
    lines += 1;
    assert.equal(chunk.endsWith("\n"), true, "every exported chunk is one JSONL line");
    const parsed = JSON.parse(chunk) as { type?: string };
    assert.notEqual(parsed.type, undefined);
  }
  assert.equal(lines, 1 + 3 * 500 + 1);

  const snapshot = await collectSnapshot(store.exportBackup("backup-stream-123"));
  assert.equal(snapshot.split("\n").filter((line) => line.length > 0).length, lines);
});
