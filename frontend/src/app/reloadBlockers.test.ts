import { afterEach, describe, expect, it, vi } from "vitest";

import {
  flushReloadPersistence,
  getReloadFlusherFailureLabel,
  registerReloadFlusher,
  resetReloadCoordinationForTests,
  retryFailedFlushers,
} from "./reloadBlockers";

afterEach(() => {
  resetReloadCoordinationForTests();
});

describe("reload persistence flush reports", () => {
  it("captures per-flusher results by id and preserves registration order in failedIds", () => {
    registerReloadFlusher("alpha", () => ({ ok: true, revision: "rev-1" }));
    registerReloadFlusher("beta", () => ({ ok: false, code: "quota-exceeded", message: "storage full" }));
    registerReloadFlusher("gamma", () => ({ ok: false, code: "verification-failed", message: "digest mismatch" }));
    registerReloadFlusher("delta", () => undefined);

    const report = flushReloadPersistence();

    expect(report.ok).toBe(false);
    expect(report.failedIds).toEqual(["beta", "gamma"]);
    expect(report.results).toEqual({
      alpha: { ok: true, revision: "rev-1" },
      beta: { ok: false, code: "quota-exceeded", message: "storage full" },
      gamma: { ok: false, code: "verification-failed", message: "digest mismatch" },
      delta: { ok: true },
    });
  });

  it("normalizes a thrown exception into a failed result without rethrowing", () => {
    registerReloadFlusher("broken", () => {
      throw new Error("quota exceeded");
    });
    registerReloadFlusher("fine", () => undefined);

    const report = flushReloadPersistence();

    expect(report.ok).toBe(false);
    expect(report.failedIds).toEqual(["broken"]);
    expect(report.results.broken).toEqual({ ok: false, code: "unknown", message: "quota exceeded" });
    expect(report.results.fine).toEqual({ ok: true });
  });

  it("runs every flusher even when an earlier one throws", () => {
    const reached = vi.fn(() => undefined);
    registerReloadFlusher("broken", () => {
      throw new Error("boom");
    });
    registerReloadFlusher("reached", reached);

    const report = flushReloadPersistence();

    expect(reached).toHaveBeenCalledTimes(1);
    expect(report.failedIds).toEqual(["broken"]);
  });

  it("retries only the listed failed ids that are still registered", () => {
    const healthy = vi.fn(() => undefined);
    const flaky = vi.fn()
      .mockImplementationOnce(() => {
        throw new Error("boom");
      })
      .mockImplementation(() => undefined);
    registerReloadFlusher("healthy", healthy);
    registerReloadFlusher("flaky", flaky);

    const first = flushReloadPersistence();
    expect(first.failedIds).toEqual(["flaky"]);

    const retry = retryFailedFlushers([...first.failedIds, "unregistered-id"]);

    expect(retry.ok).toBe(true);
    expect(retry.failedIds).toEqual([]);
    expect(retry.results).toEqual({ flaky: { ok: true } });
    expect(healthy).toHaveBeenCalledTimes(1);
    expect(flaky).toHaveBeenCalledTimes(2);
  });

  it("keeps ids in the retry report when the retry fails again", () => {
    registerReloadFlusher("still-broken", () => {
      throw new Error("still no");
    });

    const retry = retryFailedFlushers(["still-broken"]);

    expect(retry.ok).toBe(false);
    expect(retry.failedIds).toEqual(["still-broken"]);
    expect(retry.results["still-broken"]).toEqual({ ok: false, code: "unknown", message: "still no" });
  });

  it("returns an empty ok report when nothing is registered", () => {
    expect(flushReloadPersistence()).toEqual({ ok: true, results: {}, failedIds: [] });
    expect(retryFailedFlushers(["missing"])).toEqual({ ok: true, results: {}, failedIds: [] });
  });

  it("registers and returns failure labels, clearing them on unregister", () => {
    const unregister = registerReloadFlusher("composer-draft", () => undefined, { failureLabel: "草稿保存失败" });
    registerReloadFlusher("conversation", () => undefined);

    expect(getReloadFlusherFailureLabel("composer-draft")).toBe("草稿保存失败");
    expect(getReloadFlusherFailureLabel("conversation")).toBeUndefined();

    unregister();
    expect(getReloadFlusherFailureLabel("composer-draft")).toBeUndefined();
  });
});
