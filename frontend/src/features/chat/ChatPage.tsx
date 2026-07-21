import { useEffect, useRef, useState } from "react";

import { getShareTarget, readShareIdFromLocation, stripShareIdFromLocation } from "../../api/shareTargetApi";
import { useAttachments } from "../../contexts/AttachmentsContext";
import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { useSettings } from "../../contexts/SettingsContext";
import { useOnlineStatus } from "../../shared/useOnlineStatus";
import { Composer } from "../composer/Composer";
import { ActivityDrawer } from "../activity/ActivityDrawer";
import { DiagnosticsDrawer } from "../diagnostics/DiagnosticsDrawer";
import { FilePreviewDrawer } from "../file-reader/FilePreviewDrawer";
import { ImageLightbox } from "../file-reader/ImageLightbox";
import { HistoryDrawer } from "../history/HistoryDrawer";
import { MemoryDrawer } from "../memory/MemoryDrawer";
import { MemorySuggestionToast } from "../memory/MemorySuggestionToast";
import { ProjectsDrawer } from "../projects/ProjectsDrawer";
import { RemindersDrawer } from "../reminders/RemindersDrawer";
import { ConnectionSettingsDrawer } from "../settings/ConnectionSettingsDrawer";
import { SkillsDrawer } from "../skills/SkillsDrawer";
import { useReminderPolling } from "../reminders/useReminderPolling";
import { MessageList } from "./MessageList";
import { Icon } from "../../shared/ui/Icon";

function transferHasFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes("Files");
}

export function ChatPage() {
  const chat = useChat();
  const overlay = useOverlay();
  const settings = useSettings();
  const attachments = useAttachments();
  const online = useOnlineStatus();
  const [suggestedPrompt, setSuggestedPrompt] = useState("");
  const [dropActive, setDropActive] = useState(false);
  const dragDepthRef = useRef(0);
  const shareConsumedRef = useRef(false);
  const connected = Boolean(settings.runtime?.hasServerKey || settings.apiKey.trim());
  const conversationId = chat.state.currentConversationId;
  useReminderPolling(chat.notify);

  useEffect(() => {
    attachments.clear();
  }, [conversationId]);

  useEffect(() => {
    if (shareConsumedRef.current) return;
    shareConsumedRef.current = true;
    const shareId = readShareIdFromLocation(window.location.search);
    if (!shareId) return;
    window.history.replaceState(
      null,
      "",
      stripShareIdFromLocation(window.location.pathname, window.location.search, window.location.hash),
    );
    void getShareTarget(shareId)
      .then((share) => {
        if (!share.prompt && !share.attachments.length) return;
        const summary = `导入这次分享内容到当前草稿？${share.attachments.length ? `\n附件：${share.attachments.length} 个` : ""}${share.errors.length ? `\n未识别文件：${share.errors.length} 个` : ""}${share.prompt ? `\n\n${share.prompt.slice(0, 120)}${share.prompt.length > 120 ? "…" : ""}` : ""}`;
        if (!window.confirm(summary)) return;
        if (share.prompt) setSuggestedPrompt(share.prompt);
        if (share.attachments.length) attachments.addReadyAttachments(share.attachments);
      })
      .catch((reason: unknown) => {
        chat.notify(reason instanceof Error && reason.message ? reason.message : "读取分享内容失败");
      });
  }, [attachments, chat]);

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
          <button className="topbar-icon history-trigger" type="button" aria-label="打开历史" onClick={() => overlay.openOverlay("history")}><Icon name="menu" /></button>
          <div className="topbar-title">
            <strong>{chat.state.currentConversationId ? "当前对话" : "新对话"}</strong>
            <span>
              <i className={connected && online ? "status-ok" : "status-warn"} />
              {online ? (connected ? "对话后端可用" : "需要 API Key") : "离线模式，稍后可重试"}
            </span>
          </div>
          <div className="topbar-actions">
            <button className="topbar-icon" type="button" aria-label="连接设置" onClick={() => overlay.openOverlay("settings")}><Icon name="settings" /></button>
          </div>
        </header>
        <MessageList messages={chat.messages} onSuggestion={setSuggestedPrompt} />
        {chat.state.notice && (
          <button className="chat-notice" type="button" onClick={chat.clearNotice}>{chat.state.notice}<span><Icon name="close" /></span></button>
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
      <ProjectsDrawer />
      <SkillsDrawer />
      <MemoryDrawer />
      <RemindersDrawer />
      <DiagnosticsDrawer />
      <FilePreviewDrawer />
      <ImageLightbox />
      <ActivityDrawer />
      <MemorySuggestionToast />
    </main>
  );
}
