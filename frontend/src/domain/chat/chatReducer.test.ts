import { describe, expect, it } from "vitest";

import { INTERRUPTED_CHECKPOINT_NOTE } from "../conversation/checkpoint";
import { chatReducer, createInitialChatState } from "./chatReducer";
import { createAssistantMessage } from "./streamReducer";
import type { ChatMessage } from "./types";

function userMessage(): ChatMessage {
  return {
    id: "user-1",
    role: "user",
    content: "hello",
    reasoning: "",
    createdAt: 1,
    phase: "done",
    streaming: false,
    attachments: [],
    timeline: [],
    systemNotes: [],
  };
}

describe("chatReducer", () => {
  it("creates a conversation, applies stream events, and settles the request", () => {
    const initial = createInitialChatState({ schemaVersion: 1, currentConversationId: null, conversations: [] });
    const started = chatReducer(initial, {
      type: "requestStarted",
      conversationId: "conversation-1",
      userMessage: userMessage(),
      assistantMessage: createAssistantMessage("assistant-1"),
      model: "deepseek-v4-pro",
      thinkingEnabled: true,
    });
    const content = chatReducer(started, {
      type: "streamEventReceived",
      messageId: "assistant-1",
      event: { type: "content", text: "world" },
    });
    const done = chatReducer(content, {
      type: "streamEventReceived",
      messageId: "assistant-1",
      event: { type: "done" },
    });

    expect(started).toMatchObject({ currentConversationId: "conversation-1", requestStatus: "streaming" });
    expect(done.requestStatus).toBe("idle");
    expect(done.conversations[0]?.messages[1]).toMatchObject({ content: "world", phase: "done", streaming: false });
  });

  it("preserves partial output when generation is stopped", () => {
    const initial = createInitialChatState({ schemaVersion: 1, currentConversationId: null, conversations: [] });
    const started = chatReducer(initial, {
      type: "requestStarted",
      conversationId: "conversation-1",
      userMessage: userMessage(),
      assistantMessage: createAssistantMessage("assistant-1"),
      model: "deepseek-v4-pro",
      thinkingEnabled: true,
    });
    const partial = chatReducer(started, {
      type: "streamEventReceived",
      messageId: "assistant-1",
      event: { type: "content", text: "partial" },
    });
    const stopped = chatReducer(partial, { type: "requestStopped", messageId: "assistant-1" });
    expect(stopped.conversations[0]?.messages[1]).toMatchObject({ content: "partial", interrupted: true, phase: "interrupted" });
  });

  function conversationState(): ReturnType<typeof createInitialChatState> {
    const initial = createInitialChatState({ schemaVersion: 1, currentConversationId: null, conversations: [] });
    const first = chatReducer(initial, {
      type: "requestStarted",
      conversationId: "conversation-1",
      userMessage: userMessage(),
      assistantMessage: createAssistantMessage("assistant-1"),
      model: "deepseek-v4-pro",
      thinkingEnabled: true,
    });
    const answered = chatReducer(first, {
      type: "streamEventReceived",
      messageId: "assistant-1",
      event: { type: "done", content: "answer-1" },
    });
    const secondUser: ChatMessage = { ...userMessage(), id: "user-2", content: "second question" };
    return chatReducer(answered, {
      type: "requestStarted",
      conversationId: "conversation-1",
      userMessage: secondUser,
      assistantMessage: createAssistantMessage("assistant-2"),
      model: "deepseek-v4-pro",
      thinkingEnabled: true,
    });
  }

  it("messageEditResubmitted truncates after the edited user message and restarts streaming", () => {
    const idle = { ...conversationState(), requestStatus: "idle" as const };
    const replacement = createAssistantMessage("assistant-3");
    const state = chatReducer(idle, {
      type: "messageEditResubmitted",
      messageId: "user-1",
      content: "edited question",
      updatedAt: 42,
      assistantMessage: replacement,
      model: "deepseek-v4-pro",
      thinkingEnabled: false,
    });
    const messages = state.conversations[0]?.messages ?? [];
    expect(messages.map((message) => message.id)).toEqual(["user-1", "assistant-3"]);
    expect(messages[0]).toMatchObject({ content: "edited question", updatedAt: 42 });
    expect(state).toMatchObject({ requestStatus: "streaming", activeAssistantId: "assistant-3" });
  });

  it("messageEditResubmitted rejects unknown or assistant targets", () => {
    const idle = { ...conversationState(), requestStatus: "idle" as const };
    const replacement = createAssistantMessage("assistant-3");
    const missing = chatReducer(idle, {
      type: "messageEditResubmitted",
      messageId: "nope",
      content: "x",
      updatedAt: 1,
      assistantMessage: replacement,
      model: "m",
      thinkingEnabled: false,
    });
    expect(missing.conversations[0]?.messages).toHaveLength(4);
    const assistantTarget = chatReducer(idle, {
      type: "messageEditResubmitted",
      messageId: "assistant-1",
      content: "x",
      updatedAt: 1,
      assistantMessage: replacement,
      model: "m",
      thinkingEnabled: false,
    });
    expect(assistantTarget.conversations[0]?.messages).toHaveLength(4);
  });

  it("assistantRegenerated truncates after the target and resets it in place", () => {
    const idle = { ...conversationState(), requestStatus: "idle" as const };
    const state = chatReducer(idle, { type: "assistantRegenerated", messageId: "assistant-1" });
    const messages = state.conversations[0]?.messages ?? [];
    expect(messages.map((message) => message.id)).toEqual(["user-1", "assistant-1"]);
    expect(messages[1]).toMatchObject({ content: "", streaming: true, phase: "idle", error: undefined, interrupted: false });
    expect(state).toMatchObject({ requestStatus: "streaming", activeAssistantId: "assistant-1" });
  });

  it("assistantRegenerated refuses the first message", () => {
    const initial = createInitialChatState({ schemaVersion: 1, currentConversationId: null, conversations: [] });
    const started = chatReducer(initial, {
      type: "requestStarted",
      conversationId: "conversation-1",
      userMessage: userMessage(),
      assistantMessage: createAssistantMessage("assistant-1"),
      model: "m",
      thinkingEnabled: false,
    });
    const idle = { ...started, requestStatus: "idle" as const };
    expect(chatReducer(idle, { type: "assistantRegenerated", messageId: "user-1" })).toBe(idle);
  });

  it("continuationStarted requires an interrupted assistant message", () => {
    const idle = { ...conversationState(), requestStatus: "idle" as const };
    expect(chatReducer(idle, { type: "continuationStarted", messageId: "assistant-1" })).toBe(idle);

    const stopped = chatReducer(idle, { type: "requestStopped", messageId: "assistant-2" });
    const continued = chatReducer(stopped, { type: "continuationStarted", messageId: "assistant-2" });
    const target = continued.conversations[0]?.messages.find((message) => message.id === "assistant-2");
    expect(target).toMatchObject({ streaming: true, interrupted: false });
    expect(continued).toMatchObject({ requestStatus: "streaming", activeAssistantId: "assistant-2" });
  });

  function agentRunState() {
    const initial = createInitialChatState({ schemaVersion: 1, currentConversationId: null, conversations: [] });
    const started = chatReducer(initial, {
      type: "requestStarted",
      conversationId: "conversation-1",
      userMessage: userMessage(),
      assistantMessage: createAssistantMessage("assistant-1"),
      model: "m",
      thinkingEnabled: false,
    });
    return chatReducer(started, {
      type: "streamEventReceived",
      messageId: "assistant-1",
      event: { type: "run_status", status: "running", runId: "run_1" },
    });
  }

  it("agentRunMissing marks the message interrupted with a deduped checkpoint note", () => {
    const once = chatReducer(agentRunState(), { type: "agentRunMissing", messageId: "assistant-1" });
    const message = once.conversations[0]?.messages[1];
    expect(message).toMatchObject({
      phase: "interrupted",
      streaming: false,
      interrupted: true,
      agentRunStatus: "cancelled",
    });
    expect(message?.systemNotes).toEqual([INTERRUPTED_CHECKPOINT_NOTE]);
    expect(once).toMatchObject({ requestStatus: "idle", activeAssistantId: null });

    const twice = chatReducer(once, { type: "agentRunMissing", messageId: "assistant-1" });
    expect(twice.conversations[0]?.messages[1]?.systemNotes).toEqual([INTERRUPTED_CHECKPOINT_NOTE]);
  });

  it("agentRunOrphaned stops streaming and exposes the orphaned status without touching content", () => {
    const state = chatReducer(agentRunState(), {
      type: "streamEventReceived",
      messageId: "assistant-1",
      event: { type: "content", text: "半截" },
    });
    const orphaned = chatReducer(state, { type: "agentRunOrphaned", messageId: "assistant-1" });
    const message = orphaned.conversations[0]?.messages[1];
    expect(message).toMatchObject({ streaming: false, agentRunStatus: "orphaned", content: "半截" });
    expect(message?.interrupted).not.toBe(true);
    expect(orphaned).toMatchObject({ requestStatus: "idle", activeAssistantId: null });
  });
});
