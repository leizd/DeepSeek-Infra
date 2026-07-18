import { useState, type FormEvent, type KeyboardEvent } from "react";

import { useAttachments } from "../../contexts/AttachmentsContext";
import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { useSettings } from "../../contexts/SettingsContext";

export function useComposer() {
  const [value, setValue] = useState("");
  const chat = useChat();
  const settings = useSettings();
  const overlay = useOverlay();
  const attachments = useAttachments();

  function submit() {
    const content = value.trim();
    if (chat.state.requestStatus === "streaming") return;
    if (attachments.state.uploading) {
      chat.notify("文件还在上传或识别，请稍等");
      return;
    }
    if (attachments.hasErrors) {
      chat.notify("请先移除识别失败的文件");
      return;
    }
    const ready = attachments.consumeReadyAttachments();
    if (!content && !ready.length) return;
    if (!settings.apiKey.trim() && !settings.runtime?.hasServerKey) {
      overlay.openOverlay("settings");
      void chat.sendMessage(content, { attachments: ready });
      return;
    }
    setValue("");
    void chat.sendMessage(content, { attachments: ready });
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    submit();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  const canSend = Boolean(value.trim()) || attachments.readyCount > 0;

  return { value, setValue, onSubmit, onKeyDown, submit, canSend };
}
