import type { ChatMessage } from "../../domain/chat/types";
import { MarkdownContent } from "../../shared/markdown/MarkdownContent";
import { SearchBlock } from "../citations/SearchBlock";

function activityLabel(message: ChatMessage): string {
  if (message.phase === "thinking") return "正在思考";
  if (message.phase === "searching") return "正在搜索";
  if (message.phase === "answering") return "正在回答";
  if (message.phase === "interrupted") return "已停止";
  if (message.phase === "error") return "请求失败";
  return message.streaming ? "处理中" : "回答完成";
}

export function AssistantMessage({
  message,
  onCitation,
}: {
  message: ChatMessage;
  onCitation?: (citationId: string) => void;
}) {
  return (
    <div className="assistant-message">
      {(message.reasoning || message.systemNotes.length > 0 || message.streaming) && (
        <details className="reasoning-panel" open={message.streaming}>
          <summary>
            <span>{activityLabel(message)}</span>
            {message.streaming && <span className="stream-dot" aria-hidden="true" />}
          </summary>
          {message.systemNotes.map((note, index) => <p className="system-note" key={index}>{note}</p>)}
          {message.reasoning && <MarkdownContent content={message.reasoning} onCitation={onCitation} />}
        </details>
      )}
      {message.search && <SearchBlock search={message.search} streaming={message.streaming} />}
      {message.content ? <MarkdownContent content={message.content} onCitation={onCitation} /> : message.streaming ? <p className="response-placeholder">等待模型输出…</p> : null}
      {message.error && <p className="message-error" role="alert">{message.error}</p>}
      {message.interrupted && <p className="message-meta">生成已由用户停止，已保留当前内容。</p>}
    </div>
  );
}
