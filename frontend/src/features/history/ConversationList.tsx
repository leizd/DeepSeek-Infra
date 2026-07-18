import { useMemo, useState, type KeyboardEvent } from "react";

import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import type { Conversation } from "../../domain/conversation/types";

function formatUpdatedAt(value: number): string {
  const date = new Date(value);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function matchesQuery(conversation: Conversation, normalized: string): boolean {
  return (
    conversation.title.toLowerCase().includes(normalized)
    || conversation.messages.some((message) => message.content.toLowerCase().includes(normalized))
  );
}

function RenameForm({ conversation, onClose }: { conversation: Conversation; onClose(): void }) {
  const chat = useChat();
  const [draft, setDraft] = useState(conversation.title);

  function submit() {
    const title = draft.trim();
    if (title && title !== conversation.title) chat.renameConversation(conversation.id, title);
    onClose();
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
    if (event.key === "Escape") onClose();
  }

  return (
    <input
      className="conversation-rename-input"
      aria-label="重命名对话"
      maxLength={80}
      autoFocus
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={onKeyDown}
      onBlur={submit}
    />
  );
}

export function ConversationList() {
  const chat = useChat();
  const overlay = useOverlay();
  const [query, setQuery] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const busy = chat.state.requestStatus === "streaming";
  const conversations = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return normalized
      ? chat.state.conversations.filter((conversation) => matchesQuery(conversation, normalized))
      : chat.state.conversations;
  }, [chat.state.conversations, query]);

  return (
    <>
      <label className="history-search">
        <span className="sr-only">搜索本地历史</span>
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索本地历史" />
      </label>
      <div className="conversation-list">
        {!conversations.length && <p className="history-empty">还没有匹配的对话</p>}
        {conversations.map((conversation) => (
          <div className={conversation.id === chat.state.currentConversationId ? "conversation-item active" : "conversation-item"} key={conversation.id}>
            {renamingId === conversation.id ? (
              <RenameForm conversation={conversation} onClose={() => setRenamingId(null)} />
            ) : (
              <button
                className="conversation-open"
                type="button"
                disabled={busy}
                onClick={() => {
                  chat.openConversation(conversation.id);
                  overlay.closeOverlay();
                }}
              >
                <span>
                  {conversation.favorite ? <i className="favorite-mark" aria-label="已收藏">★</i> : null}
                  {conversation.title}
                </span>
                <small>{formatUpdatedAt(conversation.updatedAt)} · {conversation.messages.length} 条</small>
              </button>
            )}
            <div className="conversation-item-actions">
              <button
                className={conversation.favorite ? "conversation-tool favorited" : "conversation-tool"}
                type="button"
                aria-label={conversation.favorite ? `取消收藏：${conversation.title}` : `收藏对话：${conversation.title}`}
                aria-pressed={Boolean(conversation.favorite)}
                title={conversation.favorite ? "取消收藏" : "收藏"}
                onClick={() => chat.toggleFavorite(conversation.id)}
              >
                {conversation.favorite ? "★" : "☆"}
              </button>
              <button
                className="conversation-tool"
                type="button"
                aria-label={`重命名对话：${conversation.title}`}
                title="重命名"
                onClick={() => setRenamingId(conversation.id)}
              >
                ✎
              </button>
              <button
                className="conversation-tool danger"
                type="button"
                aria-label={`删除对话：${conversation.title}`}
                title="删除本地对话"
                disabled={busy}
                onClick={() => chat.deleteConversation(conversation.id)}
              >
                ×
              </button>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
