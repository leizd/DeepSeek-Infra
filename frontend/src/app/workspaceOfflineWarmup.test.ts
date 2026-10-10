import { describe, expect, it, vi } from "vitest";

import { scheduleWorkspaceOfflineWarmup } from "./workspaceOfflineWarmup";

const identity = {
  type: "build_identity" as const,
  buildId: "0123456789abcdef",
  assetSetDigest: "a".repeat(64),
  cacheReady: true,
};

describe("Workspace offline warmup", () => {
  it("does not warm on Save-Data connections", () => {
    const postMessage = vi.fn();
    const requestIdleCallback = vi.fn();
    expect(scheduleWorkspaceOfflineWarmup(
      { postMessage },
      identity,
      { connection: { saveData: true, effectiveType: "4g" } },
      { requestIdleCallback, setTimeout: vi.fn() },
    )).toBe(false);
    expect(requestIdleCallback).not.toHaveBeenCalled();
    expect(postMessage).not.toHaveBeenCalled();
  });

  it.each(["slow-2g", "2g"])("does not warm on %s connections", (effectiveType) => {
    const postMessage = vi.fn();
    const requestIdleCallback = vi.fn();
    expect(scheduleWorkspaceOfflineWarmup(
      { postMessage },
      identity,
      { connection: { effectiveType } },
      { requestIdleCallback, setTimeout: vi.fn() },
    )).toBe(false);
    expect(requestIdleCallback).not.toHaveBeenCalled();
    expect(postMessage).not.toHaveBeenCalled();
  });

  it("waits for idle time and requests only the primary Workspace layer", () => {
    const postMessage = vi.fn();
    let idleCallback: (() => void) | undefined;
    const requestIdleCallback = vi.fn((callback: () => void) => {
      idleCallback = callback;
      return 1;
    });
    expect(scheduleWorkspaceOfflineWarmup(
      { postMessage },
      identity,
      { connection: { effectiveType: "4g" } },
      { requestIdleCallback, setTimeout: vi.fn() },
    )).toBe(true);
    expect(requestIdleCallback).toHaveBeenCalledWith(expect.any(Function), { timeout: 5000 });
    expect(postMessage).not.toHaveBeenCalled();
    idleCallback?.();
    expect(postMessage).toHaveBeenCalledWith({
      type: "cache_workspace_primary",
      buildId: identity.buildId,
      assetSetDigest: identity.assetSetDigest,
    });
  });

  it("does not post to a controller that stopped controlling the page while idle", () => {
    const postMessage = vi.fn();
    let idleCallback: (() => void) | undefined;
    const requestIdleCallback = vi.fn((callback: () => void) => {
      idleCallback = callback;
      return 1;
    });
    expect(scheduleWorkspaceOfflineWarmup(
      { postMessage },
      identity,
      {},
      { requestIdleCallback, setTimeout: vi.fn() },
      () => false,
    )).toBe(true);
    idleCallback?.();
    expect(postMessage).not.toHaveBeenCalled();
  });
});
