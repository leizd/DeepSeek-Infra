import type { ChatMessage, JsonRecord } from "../chat/types";

export interface Conversation {
  id: string;
  title: string;
  messages: readonly ChatMessage[];
  createdAt: string;
  updatedAt: string;
  favorite?: boolean;
  tags?: readonly string[];
  projectId?: string;
  metadata?: JsonRecord;
}

export interface ConversationIndexEntry {
  id: string;
  title: string;
  updatedAt: string;
  favorite: boolean;
  tags: readonly string[];
  messageCount: number;
}

export interface PersistedConversationState {
  schemaVersion: 1;
  currentConversationId: string | null;
  conversations: readonly Conversation[];
}
