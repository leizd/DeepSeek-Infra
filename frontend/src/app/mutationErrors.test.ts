import { describe, expect, it } from "vitest";

import type { LifecycleMutationMeta } from "./mutationLifecycle";
import {
  latestUnresolvedLifecycleError,
  type LifecycleMutationSnapshot,
} from "./mutationErrors";

function snapshot(
  entityKey: string,
  status: string,
  submittedAt: number,
  error: unknown = null,
): LifecycleMutationSnapshot {
  const meta: LifecycleMutationMeta = {
    owner: "project-list",
    lifecycleId: `lifecycle-${submittedAt}`,
    entityKey,
    operation: "rename",
    intentKey: String(submittedAt),
  };
  return { status, submittedAt, error, meta };
}

describe("latestUnresolvedLifecycleError", () => {
  it("keeps a late failure visible when another entity succeeds", () => {
    const failure = new Error("项目 A 失败");
    expect(latestUnresolvedLifecycleError([
      snapshot("project:a", "error", 1, failure),
      snapshot("project:b", "success", 2),
    ])).toBe(failure);
  });

  it("resolves an entity's old failure after that entity succeeds", () => {
    expect(latestUnresolvedLifecycleError([
      snapshot("project:a", "error", 1, new Error("旧失败")),
      snapshot("project:b", "error", 2, new Error("其他失败")),
      snapshot("project:a", "success", 3),
    ])).toMatchObject({ message: "其他失败" });
  });

  it("uses the later cache entry when timestamps are equal", () => {
    expect(latestUnresolvedLifecycleError([
      snapshot("project:a", "error", 1, new Error("旧失败")),
      snapshot("project:a", "success", 1),
    ])).toBeUndefined();
  });
});
