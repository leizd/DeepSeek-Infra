import { useEffect, useRef, type ClipboardEvent } from "react";

import { useAttachments } from "../../contexts/AttachmentsContext";
import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { useSettings } from "../../contexts/SettingsContext";
import { AttachmentList } from "../attachments/AttachmentList";
import { ATTACHMENT_ACCEPT } from "../attachments/attachmentMapper";
import { ModelSelector } from "./ModelSelector";
import { useComposer } from "./useComposer";

export function Composer({ initialPrompt, onInitialPromptUsed }: { initialPrompt: string; onInitialPromptUsed(): void }) {
  const chat = useChat();
  const overlay = useOverlay();
  const settings = useSettings();
  const attachments = useAttachments();
  const composer = useComposer();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const busy = chat.state.requestStatus === "streaming";

  useEffect(() => {
    if (!initialPrompt) return;
    composer.setValue(initialPrompt);
    onInitialPromptUsed();
  }, [initialPrompt, onInitialPromptUsed]);

  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = event.clipboardData?.files;
    if (!files?.length) return;
    event.preventDefault();
    attachments.addFiles(files);
  }

  return (
    <form className="composer" onSubmit={composer.onSubmit}>
      <AttachmentList />
      <textarea
        id="reactPromptInput"
        aria-label="输入消息"
        placeholder="给 DeepSeek 发送消息"
        rows={1}
        value={composer.value}
        onChange={(event) => composer.setValue(event.target.value)}
        onKeyDown={composer.onKeyDown}
        onPaste={onPaste}
        disabled={busy}
      />
      <input
        ref={fileInputRef}
        className="sr-only"
        type="file"
        multiple
        accept={ATTACHMENT_ACCEPT}
        aria-label="添加附件"
        tabIndex={-1}
        onChange={(event) => {
          if (event.target.files?.length) attachments.addFiles(event.target.files);
          event.target.value = "";
        }}
      />
      <div className="composer-toolbar">
        <div className="composer-options">
          <button
            className="option-button"
            type="button"
            aria-label="添加附件"
            disabled={attachments.state.uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            附件
          </button>
          <ModelSelector />
          <button
            className={settings.thinkingEnabled ? "option-button active" : "option-button"}
            type="button"
            aria-pressed={settings.thinkingEnabled}
            onClick={() => settings.setThinkingEnabled(!settings.thinkingEnabled)}
          >
            思考
          </button>
          <button
            className={settings.searchEnabled ? "option-button active" : "option-button"}
            type="button"
            aria-pressed={settings.searchEnabled}
            onClick={() => settings.setSearchEnabled(!settings.searchEnabled)}
          >
            联网
          </button>
          <button className="option-button" type="button" onClick={() => overlay.openOverlay("settings")}>连接设置</button>
        </div>
        {busy ? (
          <button className="stop-button" type="button" onClick={chat.stopGeneration}>停止生成</button>
        ) : (
          <button className="send-button" type="submit" disabled={!composer.canSend} aria-label="发送消息">发送</button>
        )}
      </div>
    </form>
  );
}
