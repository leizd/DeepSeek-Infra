// @vitest-environment jsdom

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { chatStub, settingsStub, overlayStub, attachmentsStub, onlineStub } = vi.hoisted(() => ({
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
  onlineStub: { online: true },
}));

vi.mock("../../contexts/ChatContext", () => ({ useChat: () => chatStub }));
vi.mock("../../contexts/SettingsContext", () => ({ useSettings: () => settingsStub }));
vi.mock("../../contexts/OverlayContext", () => ({ useOverlay: () => overlayStub }));
vi.mock("../../contexts/AttachmentsContext", () => ({ useAttachments: () => attachmentsStub }));
vi.mock("../../contexts/ProjectsContext", () => ({ useProjects: () => ({ activeProject: null }) }));
vi.mock("../../shared/useOnlineStatus", () => ({ useOnlineStatus: () => onlineStub.online }));

import { resetReloadCoordinationForTests } from "../../app/reloadBlockers";
import { useComposer } from "./useComposer";

beforeEach(() => {
  window.sessionStorage.clear();
  resetReloadCoordinationForTests();
  chatStub.notify.mockClear();
  chatStub.tryStartMessage.mockReset();
  overlayStub.openOverlay.mockClear();
  attachmentsStub.peekReadyAttachments.mockReset().mockReturnValue([]);
  attachmentsStub.commitReadyAttachments.mockClear();
  attachmentsStub.state.uploading = false;
  attachmentsStub.hasErrors = false;
  settingsStub.apiKey = "sk-test";
  settingsStub.runtime = null;
  onlineStub.online = true;
});

afterEach(() => {
  cleanup();
  resetReloadCoordinationForTests();
});

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
