import type { ChatMessage } from "../chat/types";
import { createId } from "../../shared/createId";
import { createConversation, sortConversations } from "./reducer";
import { DEFAULT_MODEL, migrateLegacyConversation, migrateLegacyMessage } from "./migration";
import type { PersistedConversationState } from "./types";

export const conversationStorageKeys = {
  conversations: "deepseek-infra.conversations",
  currentConversation: "deepseek-infra.current-conversation",
  legacyMessages: "deepseek-infra.messages",
} as const;

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function browserStorage(): StorageLike | null {
  return typeof window === "undefined" ? null : window.localStorage;
}

function parseArray(raw: string | null): unknown[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function loadPersistedConversationState(storage: StorageLike | null = browserStorage()): PersistedConversationState {
  if (!storage) return { schemaVersion: 1, currentConversationId: null, conversations: [] };
  let conversations = parseArray(storage.getItem(conversationStorageKeys.conversations))
    .map(migrateLegacyConversation)
    .filter((conversation): conversation is NonNullable<typeof conversation> => Boolean(conversation));

  if (!conversations.length) {
    const messages = parseArray(storage.getItem(conversationStorageKeys.legacyMessages))
      .map(migrateLegacyMessage)
      .filter((message): message is ChatMessage => Boolean(message));
    if (messages.length) {
      conversations = [createConversation(createId("legacy-conversation"), messages, DEFAULT_MODEL, true)];
    }
  }

  conversations = sortConversations(conversations);
  const requestedId = storage.getItem(conversationStorageKeys.currentConversation);
  const currentConversationId = conversations.some((conversation) => conversation.id === requestedId)
    ? requestedId
    : conversations[0]?.id ?? null;
  return { schemaVersion: 1, currentConversationId, conversations };
}

export function savePersistedConversationState(
  state: PersistedConversationState,
  storage: StorageLike | null = browserStorage(),
): void {
  if (!storage) return;
  const conversations = sortConversations(state.conversations)
    .filter((conversation) => conversation.messages.length)
    .map((conversation) => ({
      ...conversation,
      messages: conversation.messages.slice(-80).map((message) => ({ ...message, streaming: false })),
    }));
  storage.setItem(conversationStorageKeys.conversations, JSON.stringify(conversations));
  if (state.currentConversationId) storage.setItem(conversationStorageKeys.currentConversation, state.currentConversationId);
  else storage.removeItem(conversationStorageKeys.currentConversation);
}
