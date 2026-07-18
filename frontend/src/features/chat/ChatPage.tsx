import { useEffect, useRef, useState } from "react";

import { useAttachments } from "../../contexts/AttachmentsContext";
import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { useSettings } from "../../contexts/SettingsContext";
import { Composer } from "../composer/Composer";
import { FilePreviewDrawer } from "../file-reader/FilePreviewDrawer";
import { ImageLightbox } from "../file-reader/ImageLightbox";
import { HistoryDrawer } from "../history/HistoryDrawer";
import { ConnectionSettingsDrawer } from "../settings/ConnectionSettingsDrawer";
import { MessageList } from "./MessageList";

function transferHasFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes("Files");
}

export function ChatPage() {
  const chat = useChat();
  const overlay = useOverlay();
  const settings = useSettings();
  const attachments = useAttachments();
  const [suggestedPrompt, setSuggestedPrompt] = useState("");
  const [dropActive, setDropActive] = useState(false);
  const dragDepthRef = useRef(0);
  const connected = Boolean(settings.runtime?.hasServerKey || settings.apiKey.trim());
  const conversationId = chat.state.currentConversationId;

  useEffect(() => {
    attachments.clear();
  }, [conversationId]);

  useEffect(() => {
    function onDragEnter(event: DragEvent) {
      if (!transferHasFiles(event)) return;
      event.preventDefault();
      dragDepthRef.current += 1;
      setDropActive(true);
    }
    function onDragOver(event: DragEvent) {
      if (!transferHasFiles(event)) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    }
    function onDragLeave(event: DragEvent) {
      if (!transferHasFiles(event)) return;
      dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
      if (!dragDepthRef.current) setDropActive(false);
    }
    function onDrop(event: DragEvent) {
      if (!transferHasFiles(event)) return;
      event.preventDefault();
      dragDepthRef.current = 0;
      setDropActive(false);
      const files = event.dataTransfer?.files;
      if (files?.length) attachments.addFiles(files);
    }
    document.addEventListener("dragenter", onDragEnter);
    document.addEventListener("dragover", onDragOver);
    document.addEventListener("dragleave", onDragLeave);
    document.addEventListener("drop", onDrop);
    return () => {
      document.removeEventListener("dragenter", onDragEnter);
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("dragleave", onDragLeave);
      document.removeEventListener("drop", onDrop);
    };
  }, [attachments]);

  return (
    <main className="chat-app-shell">
      <HistoryDrawer />
      <section className="chat-workspace">
        <header className="chat-topbar">
          <button className="topbar-icon history-trigger" type="button" aria-label="打开历史" onClick={() => overlay.openOverlay("history")}>☰</button>
          <div className="topbar-title">
            <strong>{chat.state.currentConversationId ? "当前对话" : "新对话"}</strong>
            <span><i className={connected ? "status-ok" : "status-warn"} />{connected ? "对话后端可用" : "需要 API Key"}</span>
          </div>
          <div className="topbar-actions">
            <span className="migration-badge">4.0.4 · React Chat</span>
            <button className="topbar-icon" type="button" aria-label="连接设置" onClick={() => overlay.openOverlay("settings")}>⚙</button>
          </div>
        </header>
        <MessageList messages={chat.messages} onSuggestion={setSuggestedPrompt} />
        {chat.state.notice && (
          <button className="chat-notice" type="button" onClick={chat.clearNotice}>{chat.state.notice}<span>×</span></button>
        )}
        <div className="composer-zone">
          <Composer initialPrompt={suggestedPrompt} onInitialPromptUsed={() => setSuggestedPrompt("")} />
          <p className="composer-disclaimer">AI 可能会犯错，请核对重要信息。密钥不会被浏览器长期保存。</p>
        </div>
      </section>
      {dropActive && (
        <div className="drop-overlay" aria-hidden="true">
          <div>松开以上传附件</div>
        </div>
      )}
      {overlay.activeOverlay && <button className="overlay-backdrop" type="button" aria-label="关闭浮层" onClick={overlay.closeOverlay} />}
      <ConnectionSettingsDrawer />
      <FilePreviewDrawer />
      <ImageLightbox />
    </main>
  );
}
