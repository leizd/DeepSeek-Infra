// @vitest-environment jsdom

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { chatStub, settingsStub, overlayStub, attachmentsStub, projectsStub, onlineStub } = vi.hoisted(() => ({
  chatStub: {
    state: { requestStatus: "idle", currentConversationId: "c1" as string | null },
    quoteDraft: null,
    notify: vi.fn(),
    tryStartMessage: vi.fn(),
  },
  settingsStub: { apiKey: "", runtime: null as null | { hasServerKey: boolean } },
  overlayStub: { openOverlay: vi.fn() },
  attachmentsStub: {
    state: { items: [] as unknown[], uploading: false },
    hasErrors: false,
    readyCount: 0,
    peekReadyAttachments: vi.fn((): { id: string; attachment: { name: string } }[] => []),
    commitReadyAttachments: vi.fn(),
  },
  projectsStub: { activeProject: null as null | { id: string } },
  onlineStub: { online: true },
}));

vi.mock("../../contexts/ChatContext", () => ({ useChat: () => chatStub }));
vi.mock("../../contexts/SettingsContext", () => ({ useSettings: () => settingsStub }));
vi.mock("../../contexts/OverlayContext", () => ({ useOverlay: () => overlayStub }));
vi.mock("../../contexts/AttachmentsContext", () => ({ useAttachments: () => attachmentsStub }));
vi.mock("../../contexts/ProjectsContext", () => ({ useProjects: () => projectsStub }));
vi.mock("../../shared/useOnlineStatus", () => ({ useOnlineStatus: () => onlineStub.online }));

import {
  getReloadBlockerSnapshot,
  resetReloadCoordinationForTests,
  type PersistenceFlushResult,
} from "../../app/reloadBlockers";
import { composerDraftStorageKey, type ComposerDraftScope } from "./composerDraftPersistence";
import { recallDraft, resetComposerDraftRepositoryForTests } from "./composerDraftRepository";
import { useComposer } from "./useComposer";

const scopeC1: ComposerDraftScope = { conversationId: "c1", projectId: null };
const scopeC2: ComposerDraftScope = { conversationId: "c2", projectId: null };

beforeEach(() => {
  window.sessionStorage.clear();
  resetReloadCoordinationForTests();
  resetComposerDraftRepositoryForTests();
  chatStub.state.currentConversationId = "c1";
  chatStub.notify.mockClear();
  chatStub.tryStartMessage.mockReset();
  overlayStub.openOverlay.mockClear();
  attachmentsStub.peekReadyAttachments.mockReset().mockReturnValue([]);
  attachmentsStub.commitReadyAttachments.mockClear();
  attachmentsStub.state.uploading = false;
  attachmentsStub.hasErrors = false;
  projectsStub.activeProject = null;
  settingsStub.apiKey = "sk-test";
  settingsStub.runtime = null;
  onlineStub.online = true;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  resetReloadCoordinationForTests();
  resetComposerDraftRepositoryForTests();
});

function mockSetItemThrows(error: unknown): void {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw error;
  });
}

describe("useComposer atomic submission", () => {
  it("opens settings and keeps draft text and attachments when the API key is missing", () => {
    settingsStub.apiKey = "";
    attachmentsStub.peekReadyAttachments.mockReturnValue([{ id: "u1", attachment: { name: "a.txt" } }]);
    const { result } = renderHook(() => useComposer());
    act(() => result.current.setValue("写到一半的问题"));
    act(() => result.current.submit());
    expect(overlayStub.openOverlay).toHaveBeenCalledWith("settings");
    expect(chatStub.tryStartMessage).not.toHaveBeenCalled();
    expect(attachmentsStub.peekReadyAttachments).not.toHaveBeenCalled();
    expect(attachmentsStub.commitReadyAttachments).not.toHaveBeenCalled();
    expect(result.current.value).toBe("写到一半的问题");
  });

  it("keeps text and ready attachments when the submission is rejected synchronously", () => {
    chatStub.tryStartMessage.mockReturnValue({ accepted: false, reason: "busy" });
    attachmentsStub.peekReadyAttachments.mockReturnValue([{ id: "u1", attachment: { name: "a.txt" } }]);
    const { result } = renderHook(() => useComposer());
    act(() => result.current.setValue("不要丢"));
    act(() => result.current.submit());
    expect(chatStub.tryStartMessage).toHaveBeenCalledTimes(1);
    expect(attachmentsStub.commitReadyAttachments).not.toHaveBeenCalled();
    expect(result.current.value).toBe("不要丢");
  });

  it("commits attachments once and clears the draft only after acceptance", () => {
    chatStub.tryStartMessage.mockReturnValue({ accepted: true, conversationId: "c1" });
    attachmentsStub.peekReadyAttachments.mockReturnValue([
      { id: "u1", attachment: { name: "a.txt" } },
      { id: "u2", attachment: { name: "b.txt" } },
    ]);
    const { result } = renderHook(() => useComposer());
    act(() => result.current.setValue("带着附件发送"));
    act(() => result.current.submit());
    expect(chatStub.tryStartMessage).toHaveBeenCalledWith("带着附件发送", {
      attachments: [{ name: "a.txt" }, { name: "b.txt" }],
      online: true,
    });
    expect(attachmentsStub.commitReadyAttachments).toHaveBeenCalledTimes(1);
    expect(attachmentsStub.commitReadyAttachments).toHaveBeenCalledWith(["u1", "u2"]);
    expect(result.current.value).toBe("");
    // 提交成功后内存条目与存储键都被遗忘。
    expect(recallDraft(scopeC1)).toBeNull();
    expect(window.sessionStorage.getItem(composerDraftStorageKey(scopeC1))).toBeNull();
  });

  it("notifies and does not submit while offline", () => {
    onlineStub.online = false;
    const { result } = renderHook(() => useComposer());
    act(() => result.current.setValue("离线消息"));
    act(() => result.current.submit());
    expect(chatStub.notify).toHaveBeenCalledWith("当前处于离线模式，不能发送消息");
    expect(chatStub.tryStartMessage).not.toHaveBeenCalled();
    expect(result.current.value).toBe("离线消息");
  });
});

describe("useComposer lossless drafts", () => {
  it("updates the memory repository before storage and keeps text when writes fail", () => {
    vi.useFakeTimers();
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    const { result } = renderHook(() => useComposer());
    act(() => result.current.setValue("内存里的草稿"));
    // 定时器还没跑，文本已同步进内存仓库。
    expect(recallDraft(scopeC1)?.text).toBe("内存里的草稿");
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(setItemSpy).toHaveBeenCalled();
    // 写失败：文本仍以 dirty 状态留在内存，blocker 置为未保存。
    expect(recallDraft(scopeC1)).toMatchObject({ text: "内存里的草稿", dirty: true });
    expect(getReloadBlockerSnapshot()).toContainEqual(
      expect.objectContaining({ id: "composer-draft", kind: "unsaved", active: true }),
    );
  });

  it("retains the old conversation's text across scope switches when storage writes fail", () => {
    vi.useFakeTimers();
    mockSetItemThrows(new DOMException("full", "QuotaExceededError"));
    const { result, rerender } = renderHook(() => useComposer());
    act(() => result.current.setValue("会话一的草稿"));
    act(() => {
      vi.advanceTimersByTime(500);
    });

    // 切到 c2：旧作用域刷盘失败，但文本留在内存里。
    chatStub.state.currentConversationId = "c2";
    rerender();
    expect(result.current.value).toBe("");
    expect(recallDraft(scopeC1)).toMatchObject({ text: "会话一的草稿", dirty: true });

    // 新作用域输入自己的文本。
    act(() => result.current.setValue("会话二的草稿"));
    act(() => {
      vi.advanceTimersByTime(500);
    });

    // 切回 c1：从内存恢复，失败的 c2 刷盘没有覆盖任何状态。
    chatStub.state.currentConversationId = "c1";
    rerender();
    expect(result.current.value).toBe("会话一的草稿");
    expect(recallDraft(scopeC2)).toMatchObject({ text: "会话二的草稿", dirty: true });

    // 恢复后的防抖保存自动重试，再次失败也不丢内存里的文本。
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(recallDraft(scopeC1)).toMatchObject({ text: "会话一的草稿", dirty: true });
    expect(result.current.value).toBe("会话一的草稿");
  });

  it("keeps drafts separate across project switches when storage writes fail", () => {
    vi.useFakeTimers();
    mockSetItemThrows(new DOMException("denied", "SecurityError"));
    const scopeP1: ComposerDraftScope = { conversationId: "c1", projectId: "p1" };
    const { result, rerender } = renderHook(() => useComposer());
    act(() => result.current.setValue("无项目的草稿"));

    projectsStub.activeProject = { id: "p1" };
    rerender();
    expect(result.current.value).toBe("");
    act(() => result.current.setValue("项目一的草稿"));

    projectsStub.activeProject = null;
    rerender();
    expect(result.current.value).toBe("无项目的草稿");

    projectsStub.activeProject = { id: "p1" };
    rerender();
    expect(result.current.value).toBe("项目一的草稿");
    expect(recallDraft(scopeC1)).toMatchObject({ text: "无项目的草稿", dirty: true });
    expect(recallDraft(scopeP1)).toMatchObject({ text: "项目一的草稿", dirty: true });
  });

  it("writes a scheduled save to the scope that scheduled it, never the new scope", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(() => useComposer());
    act(() => result.current.setValue("待保存的旧文本"));

    // 防抖定时器还没跑就切走：scope-switch 会先把旧作用域刷盘。
    chatStub.state.currentConversationId = "c2";
    rerender();
    act(() => {
      vi.advanceTimersByTime(500);
    });

    const keyA = composerDraftStorageKey(scopeC1);
    const keyB = composerDraftStorageKey(scopeC2);
    expect(JSON.parse(window.sessionStorage.getItem(keyA) ?? "null")).toMatchObject({
      conversationId: "c1",
      text: "待保存的旧文本",
    });
    expect(window.sessionStorage.getItem(keyB)).toBeNull();
    expect(result.current.value).toBe("");
  });

  it("lets a stale debounce timer write only its own scope's key", () => {
    vi.useFakeTimers();
    const keyA = composerDraftStorageKey(scopeC1);
    const keyB = composerDraftStorageKey(scopeC2);
    // B 已存有与 A 待输入文本相同的草稿：切换后 value 不变，旧定时器不会被清理。
    const storedB = JSON.stringify({ conversationId: "c2", projectId: null, text: "相同文本", updatedAt: 1 });
    window.sessionStorage.setItem(keyB, storedB);

    const { result, rerender } = renderHook(() => useComposer());
    act(() => result.current.setValue("相同文本"));
    chatStub.state.currentConversationId = "c2";
    rerender();
    act(() => {
      vi.advanceTimersByTime(500);
    });

    expect(result.current.value).toBe("相同文本");
    // 过期定时器写的是它调度时那个作用域（c1）的键。
    expect(JSON.parse(window.sessionStorage.getItem(keyA) ?? "null")).toMatchObject({
      conversationId: "c1",
      text: "相同文本",
    });
    // B 的键保持原样，未被过期定时器触碰。
    expect(window.sessionStorage.getItem(keyB)).toBe(storedB);
  });

  it("never lets a failed flush of the old scope overwrite the new scope", () => {
    vi.useFakeTimers();
    const keyA = composerDraftStorageKey(scopeC1);
    const keyB = composerDraftStorageKey(scopeC2);
    const originalSetItem = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === keyA) throw new DOMException("full", "QuotaExceededError");
      originalSetItem.call(this, key, value);
    });

    const { result, rerender } = renderHook(() => useComposer());
    act(() => result.current.setValue("A的草稿"));
    act(() => {
      vi.advanceTimersByTime(500);
    });

    chatStub.state.currentConversationId = "c2";
    rerender();
    act(() => result.current.setValue("B的草稿"));
    act(() => {
      vi.advanceTimersByTime(500);
    });

    // A 的写入全部失败，但 B 正常落盘、未被 A 的失败触碰。
    expect(window.sessionStorage.getItem(keyA)).toBeNull();
    expect(JSON.parse(window.sessionStorage.getItem(keyB) ?? "null")).toMatchObject({
      conversationId: "c2",
      text: "B的草稿",
    });
    expect(recallDraft(scopeC1)).toMatchObject({ text: "A的草稿", dirty: true });

    // 切回 A：从内存恢复；B 的存储内容依旧完好。
    chatStub.state.currentConversationId = "c1";
    rerender();
    expect(result.current.value).toBe("A的草稿");
    expect(JSON.parse(window.sessionStorage.getItem(keyB) ?? "null")).toMatchObject({
      conversationId: "c2",
      text: "B的草稿",
    });
  });

  it("marks the memory entry persisted with the storage revision after a successful flush", () => {
    const { result } = renderHook(() => useComposer());
    act(() => result.current.setValue("带版本的草稿"));
    let flush: PersistenceFlushResult | undefined;
    act(() => {
      flush = result.current.flushDraft();
    });
    expect(flush?.ok).toBe(true);
    const revision = flush?.ok ? flush.revision : undefined;
    expect(typeof revision).toBe("string");
    expect(recallDraft(scopeC1)).toMatchObject({
      text: "带版本的草稿",
      dirty: false,
      persistedRevision: revision,
    });
    expect(JSON.parse(window.sessionStorage.getItem(composerDraftStorageKey(scopeC1)) ?? "null")).toMatchObject({
      text: "带版本的草稿",
    });
  });

  it("returns mapped failure codes from flushDraft and keeps the draft dirty", () => {
    mockSetItemThrows(new DOMException("full", "QuotaExceededError"));
    const { result } = renderHook(() => useComposer());
    act(() => result.current.setValue("放不下的草稿"));
    let flush: PersistenceFlushResult | undefined;
    act(() => {
      flush = result.current.flushDraft();
    });
    expect(flush).toMatchObject({ ok: false, code: "quota-exceeded" });
    expect(recallDraft(scopeC1)).toMatchObject({ text: "放不下的草稿", dirty: true });
    expect(getReloadBlockerSnapshot()).toContainEqual(
      expect.objectContaining({ id: "composer-draft", kind: "unsaved", active: true }),
    );
  });

  it("forgets the memory entry after the draft is cleared", () => {
    const { result } = renderHook(() => useComposer());
    act(() => result.current.setValue("马上清掉"));
    act(() => result.current.setValue(""));
    expect(recallDraft(scopeC1)).toBeNull();
    expect(window.sessionStorage.getItem(composerDraftStorageKey(scopeC1))).toBeNull();
    expect(result.current.value).toBe("");
  });
});
