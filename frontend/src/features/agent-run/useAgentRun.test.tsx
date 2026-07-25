// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { useReducer, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentRun } from "../../api/agentRunApi";
import { ApiError } from "../../api/httpClient";
import { chatReducer, createInitialChatState, type ChatState } from "../../domain/chat/chatReducer";
import type { ChatMessage } from "../../domain/chat/types";
import type { Conversation } from "../../domain/conversation/types";
import { INTERRUPTED_CHECKPOINT_NOTE } from "../../domain/conversation/checkpoint";

const {
  getRunMock,
  resumeRunMock,
  createRunMock,
  confirmPlanMock,
  rerunPhaseMock,
  streamMock,
  settingsStub,
} = vi.hoisted(() => ({
  getRunMock: vi.fn(),
  resumeRunMock: vi.fn(),
  createRunMock: vi.fn(),
  confirmPlanMock: vi.fn(),
  rerunPhaseMock: vi.fn(),
  streamMock: vi.fn(),
  settingsStub: {
    apiKey: "sk-test",
    tavilyApiKey: "",
    model: "deepseek-v4-pro",
    thinkingEnabled: false,
    searchEnabled: false,
    agentPreset: "full",
  },
}));

vi.mock("../../api/agentRunApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/agentRunApi")>();
  return {
    ...actual,
    createAgentRun: createRunMock,
    confirmAgentPlan: confirmPlanMock,
    rerunAgentPhase: rerunPhaseMock,
    resumeAgentRun: resumeRunMock,
    getAgentRun: getRunMock,
  };
});
vi.mock("./agentRunFlow", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./agentRunFlow")>();
  return { ...actual, streamAgentRunEvents: streamMock };
});
vi.mock("../../contexts/SettingsContext", () => ({ useSettings: () => settingsStub }));
vi.mock("../../contexts/ProjectsContext", () => ({
  useProjects: () => ({ chatContext: () => ({ projectAttachments: [] }) }),
}));

import { useAgentRun } from "./useAgentRun";

function run(status: AgentRun["status"], overrides: Partial<AgentRun> = {}): AgentRun {
  return { runId: "run_1", status, nextIndex: 0, plan: [], finalAnswer: "", diagnostics: {}, ...overrides };
}

function agentMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "assistant-1",
    role: "assistant",
    content: "",
    reasoning: "",
    createdAt: 1,
    phase: "agent",
    streaming: false,
    attachments: [],
    timeline: [],
    systemNotes: [],
    agentRunId: "run_1",
    agentRunStatus: "running",
    agentRunLastEventIndex: 41,
    ...overrides,
  };
}

function stateWith(message: ChatMessage): ChatState {
  const conversation: Conversation = {
    id: "conversation-1",
    title: "会话",
    messages: [message],
    model: "deepseek-v4-pro",
    thinkingEnabled: false,
    createdAt: 1,
    updatedAt: 1,
  };
  return createInitialChatState({ schemaVersion: 1, currentConversationId: conversation.id, conversations: [conversation] });
}

function renderAgentRun(initialState: ChatState) {
  return renderHook(() => {
    const [state, dispatch] = useReducer(chatReducer, initialState);
    const abortControllerRef = useRef<AbortController | null>(null);
    const agentRun = useAgentRun({
      state,
      dispatch,
      abortControllerRef,
      requestSettings: () => ({
        apiKey: "sk-test",
        tavilyApiKey: "",
        model: "deepseek-v4-pro",
        thinkingEnabled: false,
        searchEnabled: false,
        memoryEnabled: false,
      }),
      hasBackendKey: () => true,
      maybeGenerateTitle: () => Promise.resolve(),
    });
    return { state, agentRun };
  });
}

function currentMessage(result: { current: { state: ChatState } }): ChatMessage {
  const message = result.current.state.conversations[0]?.messages[0];
  if (!message) throw new Error("message missing");
  return message;
}

beforeEach(() => {
  getRunMock.mockReset();
  resumeRunMock.mockReset();
  createRunMock.mockReset();
  confirmPlanMock.mockReset();
  rerunPhaseMock.mockReset();
  streamMock.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("useAgentRun restore reconciliation", () => {
  it("re-attaches the stream from the stored cursor when the server says the run is active, without any POST", async () => {
    getRunMock.mockResolvedValue(run("running"));
    streamMock.mockResolvedValue({ lastEventIndex: 41, completed: true });

    const { result } = renderAgentRun(stateWith(agentMessage()));

    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));
    expect(getRunMock).toHaveBeenCalledTimes(1);
    expect(getRunMock).toHaveBeenCalledWith("run_1");
    expect(streamMock).toHaveBeenCalledWith(expect.objectContaining({ runId: "run_1", after: 41 }));
    // 只读恢复：绝不新建 run、绝不 resume（不向 runs 集合发任何 POST、不重放 token）。
    expect(createRunMock).not.toHaveBeenCalled();
    expect(resumeRunMock).not.toHaveBeenCalled();
    expect(confirmPlanMock).not.toHaveBeenCalled();
    expect(rerunPhaseMock).not.toHaveBeenCalled();
    await waitFor(() => expect(result.current.state.requestStatus).toBe("idle"));
  });

  it("reconciles exactly once even as dispatches re-render the hook", async () => {
    getRunMock.mockResolvedValue(run("running"));
    streamMock.mockResolvedValue({ lastEventIndex: 41, completed: true });

    const { rerender } = renderAgentRun(stateWith(agentMessage()));
    await waitFor(() => expect(streamMock).toHaveBeenCalledTimes(1));

    rerender();
    await act(async () => {
      await Promise.resolve();
    });

    expect(getRunMock).toHaveBeenCalledTimes(1);
    expect(streamMock).toHaveBeenCalledTimes(1);
  });

  it("marks the message interrupted with the checkpoint note when the server says the run is gone (404)", async () => {
    getRunMock.mockRejectedValue(new ApiError("Agent run not found", 404, { code: "NOT_FOUND" }));

    const { result } = renderAgentRun(stateWith(agentMessage()));

    await waitFor(() => expect(currentMessage(result).phase).toBe("interrupted"));
    expect(currentMessage(result)).toMatchObject({
      interrupted: true,
      streaming: false,
      agentRunStatus: "cancelled",
    });
    expect(currentMessage(result).systemNotes).toEqual([INTERRUPTED_CHECKPOINT_NOTE]);
    expect(streamMock).not.toHaveBeenCalled();
  });

  it("settles the message with the final answer when the server says the run completed", async () => {
    getRunMock.mockResolvedValue(run("done", { finalAnswer: "最终答案" }));

    const { result } = renderAgentRun(stateWith(agentMessage({ content: "半截" })));

    await waitFor(() => expect(currentMessage(result).phase).toBe("done"));
    expect(currentMessage(result)).toMatchObject({
      agentRunStatus: "done",
      streaming: false,
      content: "最终答案",
    });
    expect(streamMock).not.toHaveBeenCalled();
  });

  it("reflects the server error when the server says the run failed", async () => {
    getRunMock.mockResolvedValue(run("failed", { diagnostics: { error: "boom" } }));

    const { result } = renderAgentRun(stateWith(agentMessage()));

    await waitFor(() => expect(currentMessage(result).phase).toBe("error"));
    expect(currentMessage(result)).toMatchObject({
      agentRunStatus: "failed",
      streaming: false,
      error: "boom",
    });
    expect(streamMock).not.toHaveBeenCalled();
  });

  it("marks the message orphaned when the status fetch fails", async () => {
    getRunMock.mockRejectedValue(new Error("network down"));

    const { result } = renderAgentRun(stateWith(agentMessage()));

    await waitFor(() => expect(currentMessage(result).agentRunStatus).toBe("orphaned"));
    expect(currentMessage(result).streaming).toBe(false);
    expect(streamMock).not.toHaveBeenCalled();
  });
});

describe("useAgentRun resumeRun (恢复 Agent Run)", () => {
  it("posts resume and re-attaches the stream on success", async () => {
    getRunMock.mockResolvedValue(run("orphaned"));
    resumeRunMock.mockResolvedValue({ started: true, run: run("running") });
    streamMock.mockResolvedValue({ lastEventIndex: 41, completed: true });

    const { result } = renderAgentRun(stateWith(agentMessage({ agentRunStatus: "orphaned" })));
    // orphaned 不是活跃状态：挂载对账不会触发。
    await act(async () => {
      await Promise.resolve();
    });
    expect(getRunMock).not.toHaveBeenCalled();

    await act(async () => {
      await result.current.agentRun.resumeRun("assistant-1");
    });

    expect(getRunMock).toHaveBeenCalledWith("run_1");
    expect(resumeRunMock).toHaveBeenCalledTimes(1);
    expect(resumeRunMock.mock.calls[0]?.[0]).toBe("run_1");
    expect(createRunMock).not.toHaveBeenCalled();
    await waitFor(() => expect(streamMock).toHaveBeenCalledWith(expect.objectContaining({ runId: "run_1", after: 41 })));
  });

  it("keeps the orphaned state and surfaces a notice when resume fails", async () => {
    getRunMock.mockResolvedValue(run("orphaned"));
    resumeRunMock.mockRejectedValue(new Error("server exploded"));

    const { result } = renderAgentRun(stateWith(agentMessage({ agentRunStatus: "orphaned" })));

    await act(async () => {
      await result.current.agentRun.resumeRun("assistant-1");
    });

    expect(resumeRunMock).toHaveBeenCalledTimes(1);
    expect(streamMock).not.toHaveBeenCalled();
    expect(currentMessage(result).agentRunStatus).toBe("orphaned");
    expect(result.current.state.notice).toBe("server exploded");
  });

  it("reattaches without a POST when the server run is already active", async () => {
    getRunMock.mockResolvedValue(run("running"));
    streamMock.mockResolvedValue({ lastEventIndex: 41, completed: true });

    const { result } = renderAgentRun(stateWith(agentMessage({ agentRunStatus: "orphaned" })));

    await act(async () => {
      await result.current.agentRun.resumeRun("assistant-1");
    });

    expect(resumeRunMock).not.toHaveBeenCalled();
    await waitFor(() => expect(streamMock).toHaveBeenCalledWith(expect.objectContaining({ runId: "run_1", after: 41 })));
  });
});
