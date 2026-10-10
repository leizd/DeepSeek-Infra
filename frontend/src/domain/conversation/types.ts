import type { ChatMessage, JsonRecord } from "../chat/types";

export interface Conversation {
  id: string;
  title: string;
  messages: readonly ChatMessage[];
  model: string;
  thinkingEnabled: boolean;
  createdAt: number;
  updatedAt: number;
  customTitle?: boolean;
  autoTitleDone?: boolean;
  favorite?: boolean;
  tags?: readonly string[];
  seekId?: string;
  projectId?: string;
  metadata?: JsonRecord;
}

export interface ConversationIndexEntry {
  id: string;
  title: string;
  updatedAt: number;
  favorite: boolean;
  tags: readonly string[];
  messageCount: number;
}

export interface PersistedConversationState {
  schemaVersion: 1;
  currentConversationId: string | null;
  conversations: readonly Conversation[];
}
