import { useState, type FormEvent, type KeyboardEvent } from "react";

import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { useSettings } from "../../contexts/SettingsContext";

export function useComposer() {
  const [value, setValue] = useState("");
  const chat = useChat();
  const settings = useSettings();
  const overlay = useOverlay();

  function submit() {
    const content = value.trim();
    if (!content || chat.state.requestStatus === "streaming") return;
    if (!settings.apiKey.trim() && !settings.runtime?.hasServerKey) {
      overlay.openOverlay("settings");
      void chat.sendMessage(content);
      return;
    }
    setValue("");
    void chat.sendMessage(content);
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

  return { value, setValue, onSubmit, onKeyDown, submit };
}
