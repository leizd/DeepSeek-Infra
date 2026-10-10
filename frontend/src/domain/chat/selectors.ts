import type { ChatState } from "./chatReducer";

export function selectCurrentConversation(state: ChatState) {
  return state.conversations.find((conversation) => conversation.id === state.currentConversationId) ?? null;
}

export function selectCurrentMessages(state: ChatState) {
  return selectCurrentConversation(state)?.messages ?? [];
}

export function selectConversationCount(state: ChatState): number {
  return state.conversations.length;
}
