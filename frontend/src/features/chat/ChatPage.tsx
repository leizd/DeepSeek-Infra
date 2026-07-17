import { useState } from "react";

import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { useSettings } from "../../contexts/SettingsContext";
import { Composer } from "../composer/Composer";
import { HistoryDrawer } from "../history/HistoryDrawer";
import { ConnectionSettingsDrawer } from "../settings/ConnectionSettingsDrawer";
import { MessageList } from "./MessageList";

export function ChatPage() {
  const chat = useChat();
  const overlay = useOverlay();
  const settings = useSettings();
  const [suggestedPrompt, setSuggestedPrompt] = useState("");
  const connected = Boolean(settings.runtime?.hasServerKey || settings.apiKey.trim());

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
            <span className="migration-badge">4.0.3 · React Chat</span>
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
      {overlay.activeOverlay && <button className="overlay-backdrop" type="button" aria-label="关闭浮层" onClick={overlay.closeOverlay} />}
      <ConnectionSettingsDrawer />
    </main>
  );
}
