import type { ChatMessage } from "../chat/types";
import type { Conversation } from "./types";
import { createId } from "../../shared/createId";

const TITLE_MAX_LENGTH = 28;

export function titleFromMessages(messages: readonly ChatMessage[]): string {
  const firstUser = messages.find((message) => message.role === "user" && message.content.trim());
  const title = (firstUser?.content ?? "").replace(/\s+/g, " ").trim();
  if (!title) return "新对话";
  return title.length > TITLE_MAX_LENGTH ? `${title.slice(0, TITLE_MAX_LENGTH)}...` : title;
}

export function createConversation(
  id: string,
  messages: readonly ChatMessage[],
  model: string,
  thinkingEnabled: boolean,
): Conversation {
  const now = Date.now();
  return {
    id,
    title: titleFromMessages(messages),
    messages,
    model,
    thinkingEnabled,
    customTitle: false,
    autoTitleDone: false,
    favorite: false,
    tags: [],
    createdAt: messages[0]?.createdAt ?? now,
    updatedAt: now,
  };
}

export function replaceConversationMessages(
  conversation: Conversation,
  messages: readonly ChatMessage[],
): Conversation {
  return {
    ...conversation,
    title: conversation.customTitle ? conversation.title : titleFromMessages(messages),
    messages,
    updatedAt: Date.now(),
  };
}

export function sortConversations(conversations: readonly Conversation[]): Conversation[] {
  return [...conversations]
    .sort((left, right) => Number(right.favorite ?? false) - Number(left.favorite ?? false) || right.updatedAt - left.updatedAt)
    .slice(0, 60);
}

/** 物化一份独立副本（冲突 / 恢复副本）：新 id、标题加后缀、不继承收藏。 */
export function copyConversation(conversation: Conversation, suffix: string): Conversation {
  const now = Date.now();
  return {
    ...conversation,
    id: createId("conversation"),
    title: `${conversation.title}${suffix}`,
    customTitle: true,
    favorite: false,
    createdAt: now,
    updatedAt: now,
  };
}

export function withRenamedTitle(conversation: Conversation, title: string, now: number): Conversation {
  const trimmed = title.trim();
  if (!trimmed) return conversation;
  return { ...conversation, title: trimmed, customTitle: true, updatedAt: now };
}

export function withFavoriteToggled(conversation: Conversation, now: number): Conversation {
  return { ...conversation, favorite: !conversation.favorite, updatedAt: now };
}
