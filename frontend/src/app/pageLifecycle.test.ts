import { afterEach, describe, expect, it, vi } from "vitest";

import { startPageLifecyclePersistence, type PageLifecycleEnvironment } from "./pageLifecycle";
import {
  registerReloadFlusher,
  resetReloadCoordinationForTests,
  setReloadBlocker,
} from "./reloadBlockers";

class FakeEventTarget {
  readonly listeners = new Map<string, Set<EventListener>>();
  readonly addEventListener = vi.fn((type: string, listener: EventListener) => {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  });
  readonly removeEventListener = vi.fn((type: string, listener: EventListener) => {
    this.listeners.get(type)?.delete(listener);
  });

  dispatch(type: string, event: Event): void {
    this.listeners.get(type)?.forEach((listener) => listener(event));
  }
}

function environment() {
  const windowTarget = new FakeEventTarget();
  const documentTarget = new FakeEventTarget();
  const documentValue = {
    ...documentTarget,
    visibilityState: "visible" as DocumentVisibilityState,
  };
  const value = {
    windowValue: windowTarget,
    documentValue,
  } as unknown as PageLifecycleEnvironment;
  return { value, windowTarget, documentTarget, documentValue };
}

function beforeUnloadEvent(): BeforeUnloadEvent {
  return {
    type: "beforeunload",
    preventDefault: vi.fn(),
    returnValue: undefined,
  } as unknown as BeforeUnloadEvent;
}

afterEach(() => {
  resetReloadCoordinationForTests();
});

describe("page lifecycle persistence", () => {
  it("flushes synchronously on pagehide, including BFCache entries, without teardown", () => {
    const runtime = environment();
    const flush = vi.fn();
    const unregister = registerReloadFlusher("composer-draft", flush);
    const stop = startPageLifecyclePersistence(runtime.value);

    runtime.windowTarget.dispatch("pagehide", new Event("pagehide"));
    expect(flush).toHaveBeenCalledTimes(1);

    const persisted = new Event("pagehide") as PageTransitionEvent;
    Object.defineProperty(persisted, "persisted", { value: true });
    runtime.windowTarget.dispatch("pagehide", persisted);
    expect(flush).toHaveBeenCalledTimes(2);

    // BFCache 页面只保存状态：blocker/运行时保持可用，且从不注册 unload。
    setReloadBlocker({ id: "composer-draft", label: "草稿", kind: "unsaved", active: true });
    const event = beforeUnloadEvent();
    runtime.windowTarget.dispatch("beforeunload", event);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(flush).toHaveBeenCalledTimes(3);
    expect(runtime.windowTarget.addEventListener.mock.calls.map(([type]) => type)).not.toContain("unload");

    unregister();
    stop();
  });

  it("flushes on visibility hidden but not when visible", () => {
    const runtime = environment();
    const flush = vi.fn();
    const unregister = registerReloadFlusher("conversation", flush);
    const stop = startPageLifecyclePersistence(runtime.value);

    runtime.documentValue.visibilityState = "visible";
    runtime.documentTarget.dispatch("visibilitychange", new Event("visibilitychange"));
    expect(flush).not.toHaveBeenCalled();

    runtime.documentValue.visibilityState = "hidden";
    runtime.documentTarget.dispatch("visibilitychange", new Event("visibilitychange"));
    expect(flush).toHaveBeenCalledTimes(1);

    unregister();
    stop();
  });

  it("blocks beforeunload while unsaved or transient work is active", () => {
    const runtime = environment();
    const stop = startPageLifecyclePersistence(runtime.value);

    setReloadBlocker({ id: "composer-draft", label: "消息草稿正在保存", kind: "unsaved", active: true });
    const unsaved = beforeUnloadEvent();
    runtime.windowTarget.dispatch("beforeunload", unsaved);
    expect(unsaved.preventDefault).toHaveBeenCalledTimes(1);
    expect(unsaved.returnValue).toBe("");

    setReloadBlocker({ id: "composer-draft", label: "", kind: "unsaved", active: false });
    setReloadBlocker({ id: "chat-streaming", label: "正在生成回复", kind: "transient", active: true });
    const streaming = beforeUnloadEvent();
    runtime.windowTarget.dispatch("beforeunload", streaming);
    expect(streaming.preventDefault).toHaveBeenCalledTimes(1);

    setReloadBlocker({ id: "chat-streaming", label: "", kind: "transient", active: false });
    setReloadBlocker({ id: "attachment-upload", label: "文件上传中", kind: "transient", active: true });
    const uploading = beforeUnloadEvent();
    runtime.windowTarget.dispatch("beforeunload", uploading);
    expect(uploading.preventDefault).toHaveBeenCalledTimes(1);
    stop();
  });

  it("does not block beforeunload once every draft is persisted", () => {
    const runtime = environment();
    const flush = vi.fn();
    const unregister = registerReloadFlusher("composer-draft", flush);
    const stop = startPageLifecyclePersistence(runtime.value);

    const event = beforeUnloadEvent();
    runtime.windowTarget.dispatch("beforeunload", event);
    expect(flush).toHaveBeenCalledTimes(1);
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(event.returnValue).not.toBe("");

    unregister();
    stop();
  });

  it("stops listening after the runtime is disposed", () => {
    const runtime = environment();
    const flush = vi.fn();
    const unregister = registerReloadFlusher("composer-draft", flush);
    const stop = startPageLifecyclePersistence(runtime.value);
    stop();

    runtime.windowTarget.dispatch("pagehide", new Event("pagehide"));
    expect(flush).not.toHaveBeenCalled();
    unregister();
  });
});
