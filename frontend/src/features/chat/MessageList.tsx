import { useEffect, useRef, useState } from "react";

import type { ChatMessage } from "../../domain/chat/types";
import { useChat } from "../../contexts/ChatContext";
import { useOpenCitation } from "../citations/useOpenCitation";
import { useSpeechPlayer } from "../speech/useSpeechPlayer";
import { MessageItem } from "./MessageItem";

const suggestions = ["解释一下这个项目的架构", "给我一个分步骤的实现方案", "帮我审查一段代码"];

interface QuoteCandidate {
  messageId: string;
  text: string;
  top: number;
  left: number;
}

function selectionCandidate(): QuoteCandidate | null {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
  const text = selection.toString().trim();
  if (!text) return null;
  const node = selection.anchorNode;
  const element = node instanceof Element ? node : node?.parentElement;
  const article = element?.closest("[data-message-id]");
  const body = element?.closest(".message-body");
  if (!article || !body) return null;
  const messageId = article.getAttribute("data-message-id");
  if (!messageId) return null;
  const rect = selection.getRangeAt(0).getBoundingClientRect();
  return { messageId, text, top: rect.top, left: rect.left + rect.width / 2 };
}

export function MessageList({ messages, onSuggestion }: { messages: readonly ChatMessage[]; onSuggestion(text: string): void }) {
  const endRef = useRef<HTMLDivElement>(null);
  const openCitation = useOpenCitation(messages);
  const chat = useChat();
  const speech = useSpeechPlayer(() => chat.notify("当前浏览器不支持朗读"));
  const [candidate, setCandidate] = useState<QuoteCandidate | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  useEffect(() => {
    function onMouseUp() {
      window.setTimeout(() => setCandidate(selectionCandidate()), 0);
    }
    function onSelectionChange() {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed) setCandidate(null);
    }
    document.addEventListener("mouseup", onMouseUp);
    document.addEventListener("selectionchange", onSelectionChange);
    return () => {
      document.removeEventListener("mouseup", onMouseUp);
      document.removeEventListener("selectionchange", onSelectionChange);
    };
  }, []);

  function quoteCandidateSelection() {
    if (!candidate) return;
    const message = messages.find((item) => item.id === candidate.messageId);
    if (message) chat.quoteMessage(message, candidate.text);
    window.getSelection()?.removeAllRanges();
    setCandidate(null);
  }

  return (
    <section className="message-list" aria-live="polite" aria-label="对话消息">
      {!messages.length && (
        <div className="empty-chat">
          <div className="empty-mark">DS</div>
          <p className="eyebrow">DEEPSEEK INFRA</p>
          <h1>今天想一起解决什么？</h1>
          <p>从一个问题、文件或项目开始。</p>
          <div className="suggestion-grid">
            {suggestions.map((suggestion) => <button key={suggestion} type="button" onClick={() => onSuggestion(suggestion)}>{suggestion}</button>)}
          </div>
        </div>
      )}
      {messages.map((message) => <MessageItem key={message.id} message={message} onCitation={openCitation(message)} speech={speech} />)}
      {candidate && (
        <button
          className="quote-popover"
          type="button"
          style={{ top: candidate.top - 8, left: candidate.left }}
          onMouseDown={(event) => event.preventDefault()}
          onClick={quoteCandidateSelection}
        >
          引用
        </button>
      )}
      <div ref={endRef} />
    </section>
  );
}
