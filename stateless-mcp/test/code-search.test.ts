import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import { searchCode } from "../src/code-search.js";

test("code search returns bounded literal matches", async () => {
  const workspaceRoot = path.resolve(import.meta.dirname, "..", "..", "..");
  const result = await searchCode(
    workspaceRoot,
    {
      query: "deepseek-infra-stateless-mcp",
      path: "stateless-mcp/src",
      glob: "*.ts",
      maxResults: 10,
    },
    32_768,
  );
  assert.ok(result.matches.some((line) => line.includes("telemetry.ts")));
});
