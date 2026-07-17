import { useEffect, useRef } from "react";

import type { ChatMessage } from "../../domain/chat/types";
import { MessageItem } from "./MessageItem";

const suggestions = ["解释一下这个项目的架构", "给我一个分步骤的实现方案", "帮我审查一段代码"];

export function MessageList({ messages, onSuggestion }: { messages: readonly ChatMessage[]; onSuggestion(text: string): void }) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  return (
    <section className="message-list" aria-live="polite" aria-label="对话消息">
      {!messages.length && (
        <div className="empty-chat">
          <div className="empty-mark">DS</div>
          <p className="eyebrow">REACT CHAT · 4.0.3</p>
          <h1>今天想一起解决什么？</h1>
          <p>普通聊天、思考流、Markdown、停止生成和本地历史已经迁入 React。</p>
          <div className="suggestion-grid">
            {suggestions.map((suggestion) => <button key={suggestion} type="button" onClick={() => onSuggestion(suggestion)}>{suggestion}</button>)}
          </div>
        </div>
      )}
      {messages.map((message) => <MessageItem key={message.id} message={message} />)}
      <div ref={endRef} />
    </section>
  );
}
