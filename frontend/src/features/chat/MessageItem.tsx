import type { ChatMessage } from "../../domain/chat/types";
import { MarkdownContent } from "../../shared/markdown/MarkdownContent";
import { AssistantMessage } from "./AssistantMessage";

export function MessageItem({ message }: { message: ChatMessage }) {
  return (
    <article className={`message-row ${message.role}`} data-message-id={message.id}>
      <div className="message-avatar" aria-hidden="true">{message.role === "user" ? "你" : "DS"}</div>
      <div className="message-body">
        <div className="message-label">{message.role === "user" ? "你" : "DeepSeek"}</div>
        {message.role === "assistant" ? <AssistantMessage message={message} /> : <MarkdownContent content={message.content} />}
      </div>
    </article>
  );
}
