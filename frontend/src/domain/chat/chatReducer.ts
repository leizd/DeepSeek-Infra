import { applyStreamEvent, resetAssistantMessage } from "./streamReducer";
import type { ChatMessage, ChatStreamEvent } from "./types";
import {
  createConversation,
  replaceConversationMessages,
  sortConversations,
  withFavoriteToggled,
  withRenamedTitle,
} from "../conversation/reducer";
import type { Conversation, PersistedConversationState } from "../conversation/types";

export interface ChatState extends PersistedConversationState {
  requestStatus: "idle" | "streaming";
  activeAssistantId: string | null;
  notice: string;
}

export type ChatAction =
  | { type: "newConversation" }
  | { type: "openConversation"; conversationId: string }
  | { type: "deleteConversation"; conversationId: string }
  | {
      type: "requestStarted";
      conversationId: string;
      userMessage: ChatMessage;
      assistantMessage: ChatMessage;
      model: string;
      thinkingEnabled: boolean;
    }
  | { type: "streamEventReceived"; messageId: string; event: ChatStreamEvent }
  | { type: "requestFailed"; messageId: string; error: string }
  | { type: "requestStopped"; messageId: string }
  | {
      type: "messageEditResubmitted";
      messageId: string;
      content: string;
      updatedAt: number;
      assistantMessage: ChatMessage;
      model: string;
      thinkingEnabled: boolean;
    }
  | { type: "assistantRegenerated"; messageId: string }
  | { type: "continuationStarted"; messageId: string }
  | { type: "conversationTitleUpdated"; conversationId: string; title: string }
  | { type: "conversationRenamed"; conversationId: string; title: string; updatedAt: number }
  | { type: "conversationFavoriteToggled"; conversationId: string; updatedAt: number }
  | { type: "noticeSet"; notice: string }
  | { type: "noticeCleared" };

export function createInitialChatState(persisted: PersistedConversationState): ChatState {
  return { ...persisted, requestStatus: "idle", activeAssistantId: null, notice: "" };
}

function updateConversation(
  conversations: readonly Conversation[],
  conversationId: string,
  update: (conversation: Conversation) => Conversation,
): Conversation[] {
  return sortConversations(
    conversations.map((conversation) => (conversation.id === conversationId ? update(conversation) : conversation)),
  );
}

function activeMessages(state: ChatState): readonly ChatMessage[] {
  return state.conversations.find((conversation) => conversation.id === state.currentConversationId)?.messages ?? [];
}

function replaceMessage(state: ChatState, messageId: string, update: (message: ChatMessage) => ChatMessage): ChatState {
  if (!state.currentConversationId) return state;
  const conversations = updateConversation(state.conversations, state.currentConversationId, (conversation) =>
    replaceConversationMessages(
      conversation,
      conversation.messages.map((message) => (message.id === messageId ? update(message) : message)),
    ),
  );
  return { ...state, conversations };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "newConversation":
      if (state.requestStatus === "streaming") return state;
      return { ...state, currentConversationId: null, notice: "" };
    case "openConversation":
      if (state.requestStatus === "streaming" || !state.conversations.some((item) => item.id === action.conversationId)) return state;
      return { ...state, currentConversationId: action.conversationId, notice: "" };
    case "deleteConversation": {
      if (state.requestStatus === "streaming") return state;
      const conversations = state.conversations.filter((conversation) => conversation.id !== action.conversationId);
      const currentConversationId = state.currentConversationId === action.conversationId
        ? conversations[0]?.id ?? null
        : state.currentConversationId;
      return { ...state, conversations, currentConversationId };
    }
    case "requestStarted": {
      const existing = state.conversations.find((conversation) => conversation.id === action.conversationId);
      const messages = [...(existing?.messages ?? activeMessages(state)), action.userMessage, action.assistantMessage];
      const conversation = existing
        ? { ...replaceConversationMessages(existing, messages), model: action.model, thinkingEnabled: action.thinkingEnabled }
        : createConversation(action.conversationId, messages, action.model, action.thinkingEnabled);
      const conversations = sortConversations([
        conversation,
        ...state.conversations.filter((item) => item.id !== action.conversationId),
      ]);
      return {
        ...state,
        conversations,
        currentConversationId: action.conversationId,
        requestStatus: "streaming",
        activeAssistantId: action.assistantMessage.id,
        notice: "",
      };
    }
    case "streamEventReceived": {
      const next = replaceMessage(state, action.messageId, (message) => applyStreamEvent(message, action.event));
      const terminal = action.event.type === "done" || action.event.type === "error";
      return terminal ? { ...next, requestStatus: "idle", activeAssistantId: null } : next;
    }
    case "requestFailed": {
      const next = replaceMessage(state, action.messageId, (message) => ({
        ...message,
        phase: "error",
        streaming: false,
        error: action.error,
      }));
      return { ...next, requestStatus: "idle", activeAssistantId: null, notice: action.error };
    }
    case "requestStopped": {
      const next = replaceMessage(state, action.messageId, (message) => ({
        ...message,
        phase: "interrupted",
        streaming: false,
        interrupted: true,
      }));
      return { ...next, requestStatus: "idle", activeAssistantId: null, notice: "已停止生成" };
    }
    case "messageEditResubmitted": {
      if (state.requestStatus === "streaming" || !state.currentConversationId) return state;
      const conversation = state.conversations.find((item) => item.id === state.currentConversationId);
      if (!conversation) return state;
      const userIndex = conversation.messages.findIndex((message) => message.id === action.messageId && message.role === "user");
      if (userIndex < 0) return state;
      const messages = conversation.messages.slice(0, userIndex + 1).map((message, index) =>
        index === userIndex ? { ...message, content: action.content, updatedAt: action.updatedAt } : message,
      );
      messages.push(action.assistantMessage);
      const conversations = updateConversation(state.conversations, conversation.id, (item) => ({
        ...replaceConversationMessages(item, messages),
        model: action.model,
        thinkingEnabled: action.thinkingEnabled,
      }));
      return {
        ...state,
        conversations,
        requestStatus: "streaming",
        activeAssistantId: action.assistantMessage.id,
        notice: "",
      };
    }
    case "assistantRegenerated": {
      if (state.requestStatus === "streaming" || !state.currentConversationId) return state;
      const conversation = state.conversations.find((item) => item.id === state.currentConversationId);
      if (!conversation) return state;
      const targetIndex = conversation.messages.findIndex((message) => message.id === action.messageId && message.role === "assistant");
      if (targetIndex <= 0) return state;
      const messages = conversation.messages
        .slice(0, targetIndex + 1)
        .map((message, index) => (index === targetIndex ? resetAssistantMessage(message) : message));
      const conversations = updateConversation(state.conversations, conversation.id, (item) =>
        replaceConversationMessages(item, messages),
      );
      return {
        ...state,
        conversations,
        requestStatus: "streaming",
        activeAssistantId: action.messageId,
        notice: "",
      };
    }
    case "continuationStarted": {
      if (state.requestStatus === "streaming") return state;
      const conversation = state.conversations.find((item) => item.id === state.currentConversationId);
      const target = conversation?.messages.find((message) => message.id === action.messageId && message.role === "assistant");
      if (!target?.interrupted) return state;
      const next = replaceMessage(state, action.messageId, (message) => ({
        ...message,
        streaming: true,
        interrupted: false,
        error: undefined,
      }));
      return { ...next, requestStatus: "streaming", activeAssistantId: action.messageId, notice: "" };
    }
    case "conversationTitleUpdated":
      return {
        ...state,
        conversations: updateConversation(state.conversations, action.conversationId, (conversation) => ({
          ...conversation,
          title: action.title,
          autoTitleDone: true,
        })),
      };
    case "conversationRenamed":
      if (!action.title.trim()) return state;
      return {
        ...state,
        conversations: updateConversation(state.conversations, action.conversationId, (conversation) =>
          withRenamedTitle(conversation, action.title, action.updatedAt),
        ),
      };
    case "conversationFavoriteToggled":
      return {
        ...state,
        conversations: updateConversation(state.conversations, action.conversationId, (conversation) =>
          withFavoriteToggled(conversation, action.updatedAt),
        ),
      };
    case "noticeSet":
      return { ...state, notice: action.notice };
    case "noticeCleared":
      return { ...state, notice: "" };
  }
}
