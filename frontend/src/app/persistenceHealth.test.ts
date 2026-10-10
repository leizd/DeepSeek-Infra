import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getPersistenceHealthSnapshot,
  recordFlushReport,
  resetPersistenceHealthForTests,
  subscribePersistenceHealth,
} from "./persistenceHealth";
import type { PersistenceFlushReport } from "./reloadBlockers";

afterEach(() => {
  resetPersistenceHealthForTests();
});

describe("persistence health store", () => {
  it("records reports, exposes them in the snapshot, and notifies subscribers", () => {
    const listener = vi.fn();
    const unsubscribe = subscribePersistenceHealth(listener);
    expect(getPersistenceHealthSnapshot()).toMatchObject({
      lastReport: null,
      failedIds: [],
      lastFailureAt: null,
      healthy: true,
    });

    const report: PersistenceFlushReport = {
      ok: false,
      results: {
        conversation: { ok: false, code: "storage-unavailable", message: "localStorage denied" },
      },
      failedIds: ["conversation"],
    };
    recordFlushReport(report);

    const snapshot = getPersistenceHealthSnapshot();
    expect(snapshot.lastReport).toBe(report);
    expect(snapshot.failedIds).toEqual(["conversation"]);
    expect(snapshot.healthy).toBe(false);
    expect(snapshot.lastFailureAt).toEqual(expect.any(Number));
    expect(snapshot.lastErrors).toEqual({
      conversation: { code: "storage-unavailable", message: "localStorage denied" },
    });
    expect(listener).toHaveBeenCalledTimes(1);

    unsubscribe();
    recordFlushReport({ ok: true, results: {}, failedIds: [] });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("updates lastSuccessRevision on success and retains it across later failures", () => {
    recordFlushReport({
      ok: true,
      results: { conversation: { ok: true, revision: "rev-1" } },
      failedIds: [],
    });
    expect(getPersistenceHealthSnapshot().lastSuccessRevision).toEqual({ conversation: "rev-1" });

    recordFlushReport({
      ok: false,
      results: { conversation: { ok: false, code: "quota-exceeded", message: "storage full" } },
      failedIds: ["conversation"],
    });
    const failed = getPersistenceHealthSnapshot();
    expect(failed.healthy).toBe(false);
    expect(failed.lastSuccessRevision).toEqual({ conversation: "rev-1" });

    recordFlushReport({
      ok: true,
      results: { conversation: { ok: true, revision: "rev-2" } },
      failedIds: [],
    });
    const recovered = getPersistenceHealthSnapshot();
    expect(recovered.healthy).toBe(true);
    expect(recovered.lastSuccessRevision).toEqual({ conversation: "rev-2" });
    expect(recovered.lastErrors).toEqual({});
  });

  it("clears a flusher's last error once it succeeds again, even without a revision", () => {
    recordFlushReport({
      ok: false,
      results: { "composer-draft": { ok: false, code: "unknown", message: "boom" } },
      failedIds: ["composer-draft"],
    });
    recordFlushReport({
      ok: true,
      results: { "composer-draft": { ok: true } },
      failedIds: [],
    });

    const snapshot = getPersistenceHealthSnapshot();
    expect(snapshot.lastErrors).toEqual({});
    expect(snapshot.lastSuccessRevision).toEqual({});
  });

  it("resets to a clean snapshot for tests", () => {
    recordFlushReport({
      ok: false,
      results: { conversation: { ok: false, code: "unknown", message: "boom" } },
      failedIds: ["conversation"],
    });

    resetPersistenceHealthForTests();

    expect(getPersistenceHealthSnapshot()).toEqual({
      lastReport: null,
      failedIds: [],
      lastFailureAt: null,
      lastSuccessRevision: {},
      lastErrors: {},
      healthy: true,
    });
  });
});
