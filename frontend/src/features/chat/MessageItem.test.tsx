// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../../domain/chat/types";
import type { SpeechPlayer } from "../speech/useSpeechPlayer";

const { resumeAgentRunMock, chatStub } = vi.hoisted(() => {
  const resumeAgentRunMock = vi.fn();
  return {
    resumeAgentRunMock,
    chatStub: {
      state: { requestStatus: "idle" },
      resumeAgentRun: resumeAgentRunMock,
      continueGeneration: vi.fn(),
      regenerate: vi.fn(),
      editAndResend: vi.fn(),
      quoteMessage: vi.fn(),
    },
  };
});

vi.mock("../../contexts/ChatContext", () => ({ useChat: () => chatStub }));
vi.mock("../../contexts/DiagnosticsContext", () => ({ useDiagnostics: () => ({ openDiagnostics: vi.fn() }) }));
vi.mock("../../contexts/ActivityContext", () => ({
  useActivity: () => ({ openMessageId: null, openActivity: vi.fn(), closeActivity: vi.fn() }),
}));

import { MessageItem } from "./MessageItem";

const speech: SpeechPlayer = {
  speakingMessageId: "",
  supported: false,
  toggleSpeak: vi.fn(),
  stop: vi.fn(),
};

function orphanedMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "assistant-1",
    role: "assistant",
    content: "半截回答",
    reasoning: "",
    createdAt: 1,
    phase: "agent",
    streaming: false,
    attachments: [],
    timeline: [],
    systemNotes: [],
    agentRunId: "run_1",
    agentRunStatus: "orphaned",
    agentRunLastEventIndex: 3,
    ...overrides,
  };
}

beforeEach(() => {
  resumeAgentRunMock.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("MessageItem orphaned agent run", () => {
  it("renders 恢复 Agent Run for an orphaned run and clicking it resumes the run", () => {
    render(<MessageItem message={orphanedMessage()} speech={speech} />);

    const button = screen.getByRole("button", { name: "恢复 Agent Run" });
    fireEvent.click(button);

    expect(resumeAgentRunMock).toHaveBeenCalledTimes(1);
    expect(resumeAgentRunMock).toHaveBeenCalledWith("assistant-1");
  });

  it("does not render the resume button for non-orphaned messages", () => {
    render(<MessageItem message={orphanedMessage({ agentRunStatus: "done", phase: "done" })} speech={speech} />);
    expect(screen.queryByRole("button", { name: "恢复 Agent Run" })).toBeNull();
  });

  it("does not render the resume button while a request is streaming", () => {
    render(<MessageItem message={orphanedMessage({ streaming: true })} speech={speech} />);
    expect(screen.queryByRole("button", { name: "恢复 Agent Run" })).toBeNull();
  });
});
