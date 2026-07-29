import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import { resolveWorkspacePath } from "../src/workspace.js";

test("workspace path resolution accepts descendants", () => {
  const root = path.resolve("workspace");
  assert.equal(resolveWorkspacePath(root, "tests/test_mcp.py"), path.join(root, "tests", "test_mcp.py"));
});

test("workspace path resolution rejects traversal", () => {
  const root = path.resolve("workspace");
  assert.throws(() => resolveWorkspacePath(root, "../secret"), /within the configured workspace/u);
  assert.throws(() => resolveWorkspacePath(root, "bad\0path"), /null byte/u);
});
