import { describe, expect, it, vi } from "vitest";

import { scheduleWorkspaceOfflineWarmup } from "./workspaceOfflineWarmup";

function registration(postMessage = vi.fn()) {
  return { active: { postMessage } };
}

describe("Workspace offline warmup", () => {
  it("does not warm on Save-Data connections", () => {
    const postMessage = vi.fn();
    const requestIdleCallback = vi.fn();
    expect(scheduleWorkspaceOfflineWarmup(
      registration(postMessage),
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
      registration(postMessage),
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
      registration(postMessage),
      { connection: { effectiveType: "4g" } },
      { requestIdleCallback, setTimeout: vi.fn() },
    )).toBe(true);
    expect(requestIdleCallback).toHaveBeenCalledWith(expect.any(Function), { timeout: 5000 });
    expect(postMessage).not.toHaveBeenCalled();
    idleCallback?.();
    expect(postMessage).toHaveBeenCalledWith({ type: "cache_workspace_primary" });
  });
});
