// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { buildUpdateStore } from "../../app/buildUpdateStore";
import type { DeployedBuild, BuildUpdateEnvironment } from "../../app/buildUpdateStore";
import {
  getPersistenceHealthSnapshot,
  recordFlushReport,
  resetPersistenceHealthForTests,
} from "../../app/persistenceHealth";
import {
  flushReloadPersistence,
  registerReloadFlusher,
  resetReloadCoordinationForTests,
} from "../../app/reloadBlockers";
import type { WorkerBuildIdentity } from "../../app/workspaceOfflineWarmup";
import { BuildUpdateBanner } from "./BuildUpdateBanner";

const TARGET_BUILD = "bbbbbbbbbbbbbbbb";

function targetBuild(): DeployedBuild {
  return {
    schemaVersion: 1,
    version: "4.3.5",
    sourceRevision: "revision-bbbbbbbb",
    buildId: TARGET_BUILD,
    assetSetDigest: "b".repeat(64),
  };
}

function identity(build: DeployedBuild): WorkerBuildIdentity {
  return {
    type: "build_identity",
    buildId: build.buildId,
    assetSetDigest: build.assetSetDigest,
    cacheReady: true,
  };
}

class FakeEventTarget {
  readonly listeners = new Map<string, Set<EventListener>>();

  addEventListener = vi.fn((type: string, listener: EventListener) => {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  });

  removeEventListener = vi.fn((type: string, listener: EventListener) => {
    this.listeners.get(type)?.delete(listener);
  });
}

function storeEnvironment(build: DeployedBuild) {
  const windowTarget = new FakeEventTarget();
  const documentTarget = new FakeEventTarget();
  const fetchValue = vi.fn(() => Promise.resolve(new Response(JSON.stringify(build), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))) as unknown as typeof fetch;
  const value = {
    fetchValue,
    windowValue: {
      ...windowTarget,
      setInterval: vi.fn(() => 1),
      clearInterval: vi.fn(),
    },
    documentValue: {
      ...documentTarget,
      visibilityState: "visible" as DocumentVisibilityState,
    },
  } as unknown as BuildUpdateEnvironment;
  return { value, fetchValue };
}

afterEach(() => {
  cleanup();
  buildUpdateStore.stop();
  resetReloadCoordinationForTests();
  resetPersistenceHealthForTests();
});

describe("BuildUpdateBanner persistence failure surface", () => {
  it("stays hidden while the build is current and persistence is healthy", () => {
    const { container } = render(<BuildUpdateBanner />);
    expect(container.firstChild).toBeNull();
  });

  it("becomes visible on a failed flush report and hides again after 重新保存 succeeds", () => {
    const flusher = vi.fn()
      .mockImplementationOnce(() => {
        throw new Error("quota exceeded");
      })
      .mockImplementation(() => undefined);
    registerReloadFlusher("conversation", flusher, { failureLabel: "对话记录保存失败" });
    recordFlushReport(flushReloadPersistence());
    expect(getPersistenceHealthSnapshot().healthy).toBe(false);

    render(<BuildUpdateBanner />);

    expect(screen.getByText("本地状态保存失败，请重试")).toBeTruthy();
    expect(screen.getByText("对话记录保存失败")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "重新保存" }));

    expect(flusher).toHaveBeenCalledTimes(2);
    expect(getPersistenceHealthSnapshot().healthy).toBe(true);
    expect(screen.queryByText("对话记录保存失败")).toBeNull();
    expect(screen.queryByText("本地状态保存失败，请重试")).toBeNull();
  });

  it("keeps the failure visible when the retry fails again", () => {
    registerReloadFlusher("composer-draft", () => {
      throw new Error("sessionStorage denied");
    }, { failureLabel: "草稿保存失败" });
    recordFlushReport(flushReloadPersistence());

    render(<BuildUpdateBanner />);
    fireEvent.click(screen.getByRole("button", { name: "重新保存" }));

    expect(screen.getByText("草稿保存失败")).toBeTruthy();
    expect(getPersistenceHealthSnapshot().failedIds).toEqual(["composer-draft"]);
  });

  it("shows the failure label alongside the update actions and covers a deferred banner", async () => {
    const build = targetBuild();
    const runtime = storeEnvironment(build);
    buildUpdateStore.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(build))),
      activate: vi.fn(),
      reload: vi.fn(),
    });
    render(<BuildUpdateBanner />);
    await waitFor(() => expect(screen.getByText("更新并重新加载")).toBeTruthy());
    expect(screen.getByText(`版本 4.3.5 · ${TARGET_BUILD}`)).toBeTruthy();

    // “稍后”本来会收起横幅；持久化失败仍然把它顶出来。
    act(() => buildUpdateStore.defer());
    expect(screen.queryByText("更新并重新加载")).toBeNull();

    const flusher = vi.fn()
      .mockImplementationOnce(() => ({ ok: false as const, code: "quota-exceeded" as const, message: "storage full" }))
      .mockImplementation(() => undefined);
    registerReloadFlusher("conversation", flusher, { failureLabel: "对话记录保存失败" });
    act(() => recordFlushReport(flushReloadPersistence()));

    expect(screen.getByText("对话记录保存失败")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重新保存" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "重新保存" }));

    expect(getPersistenceHealthSnapshot().healthy).toBe(true);
    expect(screen.queryByText("对话记录保存失败")).toBeNull();
    // 重试成功后横幅回到“稍后”状态，不再凭空出现。
    expect(screen.queryByText("更新并重新加载")).toBeNull();
  });
});
