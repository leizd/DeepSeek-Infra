// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatStreamEvent } from "../../domain/chat/types";

const { streamChatMock, memorySaveMock } = vi.hoisted(() => ({
  streamChatMock: vi.fn(),
  memorySaveMock: vi.fn(),
}));

vi.mock("../../api/chatStream", () => ({ streamChat: streamChatMock }));
vi.mock("../../api/titleApi", () => ({ generateConversationTitle: vi.fn(() => Promise.resolve("")) }));
vi.mock("../../api/remindersApi", () => ({ createReminder: vi.fn(() => Promise.resolve()) }));
vi.mock("../../contexts/SettingsContext", () => ({
  useSettings: () => ({
    apiKey: "sk-test",
    tavilyApiKey: "",
    model: "deepseek-chat",
    thinkingEnabled: false,
    searchEnabled: false,
    agentMode: false,
    agentPreset: "full",
    memoryEnabled: true,
    runtime: null,
  }),
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

import { useChatController } from "./useChatController";

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
