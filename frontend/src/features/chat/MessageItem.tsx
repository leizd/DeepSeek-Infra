import { useState } from "react";

import type { ChatMessage } from "../../domain/chat/types";
import { agentExecutionReport } from "../../domain/chat/agentTimeline";
import { useChat } from "../../contexts/ChatContext";
import { useFilePreview } from "../../contexts/FilePreviewContext";
import { formatBytes } from "../attachments/attachmentMapper";
import { copyTextToClipboard, downloadTextFile, exportFilename, messageToMarkdown } from "./messageActions";
import { MarkdownContent } from "../../shared/markdown/MarkdownContent";
import { AssistantMessage } from "./AssistantMessage";

function isPreviewableImage(attachment: ChatMessage["attachments"][number]): boolean {
  return Boolean(attachment.thumbnail || attachment.imagePreview);
}

function AttachmentChip({
  attachment,
  onOpen,
}: {
  attachment: ChatMessage["attachments"][number];
  onOpen(): void;
}) {
  if (isPreviewableImage(attachment)) {
    return (
      <button className="message-attachment image" type="button" title={attachment.name} onClick={onOpen}>
        <img src={attachment.thumbnail ?? attachment.imagePreview} alt={attachment.name} />
        <span className="message-attachment-name">{attachment.name}</span>
      </button>
    );
  }
  return (
    <button className="message-attachment file" type="button" title={attachment.name} onClick={onOpen}>
      <span className="attachment-kind" aria-hidden="true">{attachment.kind ?? "file"}</span>
      <span className="message-attachment-name">{attachment.name}</span>
      {typeof attachment.size === "number" && attachment.size > 0 && (
        <span className="message-attachment-meta">{formatBytes(attachment.size)}</span>
      )}
    </button>
  );
}

function MessageAttachments({ attachments }: { attachments: ChatMessage["attachments"] }) {
  const preview = useFilePreview();
  const images = attachments.filter((attachment) => attachment.imagePreview);
  return (
    <div className="message-attachments" aria-label="消息附件">
      {attachments.map((attachment, index) => {
        const imageIndex = images.indexOf(attachment);
        return (
          <AttachmentChip
            key={attachment.fileId ?? attachment.id ?? `${attachment.name}-${index}`}
            attachment={attachment}
            onOpen={() => {
              if (imageIndex >= 0) preview.openLightbox(images, imageIndex);
              else preview.open(attachment);
            }}
          />
        );
      })}
    </div>
  );
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="message-action"
      type="button"
      disabled={!text.trim()}
      onClick={() => {
        void copyTextToClipboard(text).then((ok) => {
          if (!ok) return;
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1_200);
        });
      }}
    >
      {copied ? "已复制" : label}
    </button>
  );
}

function UserMessageEditor({ message, onClose }: { message: ChatMessage; onClose(): void }) {
  const chat = useChat();
  const [draft, setDraft] = useState(message.content);
  const busy = chat.state.requestStatus === "streaming";
  return (
    <form
      className="message-edit-form"
      onSubmit={(event) => {
        event.preventDefault();
        onClose();
        void chat.editAndResend(message.id, draft);
      }}
    >
      <textarea
        aria-label="编辑消息"
        rows={3}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        disabled={busy}
      />
      <div className="message-edit-actions">
        <button type="submit" className="message-action primary" disabled={busy || (!draft.trim() && !message.attachments.length)}>
          保存并发送
        </button>
        <button type="button" className="message-action" onClick={onClose}>取消</button>
      </div>
    </form>
  );
}

function AssistantActions({ message }: { message: ChatMessage }) {
  const chat = useChat();
  const busy = chat.state.requestStatus === "streaming";
  const agentReport = message.agentRunId ? agentExecutionReport(message) : "";
  return (
    <div className="message-actions" aria-label="回复操作">
      {message.error && (
        <button className="message-action" type="button" disabled={busy} onClick={() => void chat.regenerate(message.id)}>
          重试
        </button>
      )}
      {message.interrupted && (
        <button className="message-action" type="button" disabled={busy} onClick={() => void chat.continueGeneration(message.id)}>
          继续生成
        </button>
      )}
      <CopyButton text={message.content} label="复制" />
      <button className="message-action" type="button" disabled={busy} onClick={() => void chat.regenerate(message.id)}>
        重新生成
      </button>
      <button
        className="message-action"
        type="button"
        disabled={!message.content.trim()}
        onClick={() => downloadTextFile(exportFilename(message.content.slice(0, 24)), messageToMarkdown(message))}
      >
        导出
      </button>
      {agentReport && <CopyButton text={agentReport} label="复制 Agent 过程" />}
    </div>
  );
}

export function MessageItem({
  message,
  onCitation,
}: {
  message: ChatMessage;
  onCitation?: (citationId: string) => void;
}) {
  const chat = useChat();
  const [editing, setEditing] = useState(false);
  const busy = chat.state.requestStatus === "streaming";
  const isUser = message.role === "user";

  return (
    <article className={`message-row ${message.role}`} data-message-id={message.id}>
      <div className="message-avatar" aria-hidden="true">{isUser ? "你" : "DS"}</div>
      <div className="message-body">
        <div className="message-label">{isUser ? "你" : "DeepSeek"}</div>
        {message.attachments.length > 0 && <MessageAttachments attachments={message.attachments} />}
        {isUser && editing ? (
          <UserMessageEditor message={message} onClose={() => setEditing(false)} />
        ) : message.role === "assistant" ? (
          <AssistantMessage message={message} onCitation={onCitation} />
        ) : (
          <MarkdownContent content={message.content} onCitation={onCitation} />
        )}
        {isUser && !editing && (
          <div className="message-actions" aria-label="消息操作">
            <button className="message-action" type="button" disabled={busy} onClick={() => setEditing(true)}>
              编辑
            </button>
            <CopyButton text={message.content} label="复制" />
            {message.updatedAt ? <span className="message-edited-mark">已编辑</span> : null}
          </div>
        )}
        {message.role === "assistant" && !message.streaming && <AssistantActions message={message} />}
      </div>
    </article>
  );
}
