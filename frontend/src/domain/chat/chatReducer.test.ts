import { describe, expect, it } from "vitest";

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
});
