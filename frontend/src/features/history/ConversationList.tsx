import { useMemo, useState } from "react";

import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";

function formatUpdatedAt(value: number): string {
  const date = new Date(value);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

export function ConversationList() {
  const chat = useChat();
  const overlay = useOverlay();
  const [query, setQuery] = useState("");
  const conversations = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return normalized
      ? chat.state.conversations.filter((conversation) =>
          conversation.title.toLowerCase().includes(normalized)
          || conversation.messages.some((message) => message.content.toLowerCase().includes(normalized)),
        )
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
            <button
              className="conversation-open"
              type="button"
              disabled={chat.state.requestStatus === "streaming"}
              onClick={() => {
                chat.openConversation(conversation.id);
                overlay.closeOverlay();
              }}
            >
              <span>{conversation.title}</span>
              <small>{formatUpdatedAt(conversation.updatedAt)} · {conversation.messages.length} 条</small>
            </button>
            <button
              className="conversation-delete"
              type="button"
              aria-label={`删除对话：${conversation.title}`}
              title="删除本地对话"
              disabled={chat.state.requestStatus === "streaming"}
              onClick={() => chat.deleteConversation(conversation.id)}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </>
  );
}
