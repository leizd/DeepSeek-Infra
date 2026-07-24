// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatStreamEvent } from "../../domain/chat/types";

const { streamChatMock, memorySaveMock, settingsStub } = vi.hoisted(() => ({
  streamChatMock: vi.fn(),
  memorySaveMock: vi.fn(),
  settingsStub: {
    apiKey: "sk-test",
    tavilyApiKey: "",
    model: "deepseek-chat",
    thinkingEnabled: false,
    searchEnabled: false,
    agentMode: false,
    agentPreset: "full",
    memoryEnabled: true,
    runtime: null as null | { hasServerKey: boolean },
  },
}));

vi.mock("../../api/chatStream", () => ({ streamChat: streamChatMock }));
vi.mock("../../api/titleApi", () => ({ generateConversationTitle: vi.fn(() => Promise.resolve("")) }));
vi.mock("../../api/remindersApi", () => ({ createReminder: vi.fn(() => Promise.resolve()) }));
vi.mock("../../contexts/SettingsContext", () => ({
  useSettings: () => settingsStub,
}));
vi.mock("../../contexts/ProjectsContext", () => ({
  useProjects: () => ({ chatContext: () => ({ projectAttachments: [] }) }),
}));
vi.mock("../../contexts/MemoryContext", () => ({
  useMemory: () => ({ save: memorySaveMock }),
}));
vi.mock("../agent-run/useAgentRun", () => ({
  useAgentRun: () => ({
    sendAgentMessage: vi.fn(() => Promise.resolve()),
    confirmPlan: vi.fn(() => Promise.resolve()),
    rerunPhase: vi.fn(() => Promise.resolve()),
  }),
}));
vi.mock("../reminders/useReminderPolling", () => ({
  ensureNotificationPermission: vi.fn(() => Promise.resolve()),
}));

import { useChatController, type MessageSubmissionResult } from "./useChatController";

function suggestionStream(content: string): AsyncGenerator<ChatStreamEvent> {
  return (async function* stream() {
    yield {
      type: "memory_suggestion",
      payload: { content, category: "fact", scope: "global" },
    };
    yield { type: "done", content: "" };
  })();
}

beforeEach(() => {
  window.localStorage.clear();
  streamChatMock.mockReset();
  memorySaveMock.mockReset();
  settingsStub.apiKey = "sk-test";
  settingsStub.runtime = null;
  settingsStub.agentMode = false;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function receiveSuggestion(
  result: { current: ReturnType<typeof useChatController> },
  content: string,
): Promise<void> {
  streamChatMock.mockImplementationOnce(() => suggestionStream(content));
  await act(async () => {
    await result.current.sendMessage(`触发 ${content}`);
  });
  await waitFor(() => expect(result.current.pendingMemorySuggestion?.content).toBe(content));
}

describe("useChatController memory suggestion reconciliation", () => {
  it("does not clear a newer suggestion when an older save succeeds", async () => {
    let resolveSave!: (value: { saved: boolean; conflicts: never[] }) => void;
    memorySaveMock.mockImplementationOnce(
      () => new Promise((resolve) => {
        resolveSave = resolve;
      }),
    );
    const { result } = renderHook(() => useChatController());
    await receiveSuggestion(result, "建议 A");

    let saveAction!: Promise<void>;
    act(() => {
      saveAction = result.current.saveMemorySuggestion();
    });
    await waitFor(() => expect(memorySaveMock).toHaveBeenCalledTimes(1));
    await receiveSuggestion(result, "建议 B");

    await act(async () => {
      resolveSave({ saved: true, conflicts: [] });
      await saveAction;
    });
    expect(result.current.pendingMemorySuggestion?.content).toBe("建议 B");
  });

  it("does not apply an older suggestion's conflicts to a newer suggestion", async () => {
    let resolveSave!: (value: {
      saved: boolean;
      conflicts: { id: string; content: string; reason: string }[];
    }) => void;
    memorySaveMock.mockImplementationOnce(
      () => new Promise((resolve) => {
        resolveSave = resolve;
      }),
    );
    const { result } = renderHook(() => useChatController());
    await receiveSuggestion(result, "建议 A");

    let saveAction!: Promise<void>;
    act(() => {
      saveAction = result.current.saveMemorySuggestion();
    });
    await waitFor(() => expect(memorySaveMock).toHaveBeenCalledTimes(1));
    await receiveSuggestion(result, "建议 B");

    await act(async () => {
      resolveSave({
        saved: false,
        conflicts: [{ id: "m1", content: "旧冲突", reason: "重复" }],
      });
      await saveAction;
    });
    expect(result.current.pendingMemorySuggestion?.content).toBe("建议 B");
    expect(result.current.pendingMemorySuggestion?.conflicts).toEqual([]);
  });
});

function pendingStream(): AsyncGenerator<ChatStreamEvent> {
  return (async function* stream() {
    await new Promise<void>(() => undefined);
  })();
}

function failingStream(): AsyncGenerator<ChatStreamEvent> {
  return (async function* stream() {
    throw new Error("网络中断");
  })();
}

function doneStream(): AsyncGenerator<ChatStreamEvent> {
  return (async function* stream() {
    yield { type: "done", content: "" };
  })();
}

describe("useChatController tryStartMessage atomic acceptance", () => {
  it("rejects with missing-key without creating a message when no backend key exists", () => {
    settingsStub.apiKey = "";
    const { result } = renderHook(() => useChatController());
    let submission: MessageSubmissionResult | undefined;
    act(() => {
      submission = result.current.tryStartMessage("你好");
    });
    expect(submission).toEqual({ accepted: false, reason: "missing-key" });
    expect(streamChatMock).not.toHaveBeenCalled();
    expect(result.current.messages).toHaveLength(0);
  });

  it("rejects empty and offline submissions before touching state", () => {
    const { result } = renderHook(() => useChatController());
    act(() => {
      expect(result.current.tryStartMessage("   ")).toEqual({ accepted: false, reason: "empty" });
      expect(result.current.tryStartMessage("你好", { online: false })).toEqual({ accepted: false, reason: "offline" });
    });
    expect(streamChatMock).not.toHaveBeenCalled();
    expect(result.current.messages).toHaveLength(0);
  });

  it("accepts a rapid double submit in the same tick only once", () => {
    streamChatMock.mockImplementation(() => pendingStream());
    const { result } = renderHook(() => useChatController());
    let first: MessageSubmissionResult | undefined;
    let second: MessageSubmissionResult | undefined;
    act(() => {
      const tryStart = result.current.tryStartMessage;
      first = tryStart("双击");
      second = tryStart("双击");
    });
    expect(first).toMatchObject({ accepted: true });
    expect(second).toEqual({ accepted: false, reason: "busy" });
    expect(streamChatMock).toHaveBeenCalledTimes(1);
    expect(result.current.messages.filter((message) => message.role === "user")).toHaveLength(1);
  });

  it("keeps the accepted user message when the stream later fails and allows a new submission", async () => {
    streamChatMock.mockImplementationOnce(() => failingStream());
    const { result } = renderHook(() => useChatController());
    let submission: MessageSubmissionResult | undefined;
    act(() => {
      submission = result.current.tryStartMessage("保留这条消息");
    });
    expect(submission).toMatchObject({ accepted: true });
    await waitFor(() => expect(result.current.state.requestStatus).not.toBe("streaming"));
    expect(
      result.current.messages.some((message) => message.role === "user" && message.content === "保留这条消息"),
    ).toBe(true);

    streamChatMock.mockImplementationOnce(() => doneStream());
    let retry: MessageSubmissionResult | undefined;
    act(() => {
      retry = result.current.tryStartMessage("第二次");
    });
    expect(retry).toMatchObject({ accepted: true });
    await waitFor(() => expect(result.current.state.requestStatus).not.toBe("streaming"));
    expect(result.current.messages.filter((message) => message.role === "user")).toHaveLength(2);
  });
});
