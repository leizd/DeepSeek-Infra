import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { clearReloadBlocker, registerReloadFlusher, setReloadBlocker } from "../../app/reloadBlockers";
import { useAttachments } from "../../contexts/AttachmentsContext";
import { useChat } from "../../contexts/ChatContext";
import { useProjects } from "../../contexts/ProjectsContext";
import { useOnlineStatus } from "../../shared/useOnlineStatus";
import { useOverlay } from "../../contexts/OverlayContext";
import { useSettings } from "../../contexts/SettingsContext";
import {
  clearComposerDraft,
  loadComposerDraft,
  saveComposerDraft,
} from "./composerDraftPersistence";

const NEW_CONVERSATION_DRAFT_ID = "new";
const COMPOSER_SAVE_DELAY_MS = 120;

export function useComposer() {
  const chat = useChat();
  const settings = useSettings();
  const overlay = useOverlay();
  const attachments = useAttachments();
  const projects = useProjects();
  const online = useOnlineStatus();
  const conversationId = chat.state.currentConversationId ?? NEW_CONVERSATION_DRAFT_ID;
  const projectId = projects.activeProject?.id ?? null;
  const [value, setValueState] = useState(() => loadComposerDraft({ conversationId, projectId })?.text ?? "");
  const draftRef = useRef({ conversationId, projectId, text: value });
  const persistedTextRef = useRef(value);

  const flushDraft = useCallback(() => {
    const draft = draftRef.current;
    const saved = draft.text
      ? saveComposerDraft({ ...draft, updatedAt: Date.now() })
      : clearComposerDraft({ conversationId: draft.conversationId, projectId: draft.projectId });
    if (!saved) {
      setReloadBlocker({
        id: "composer-draft",
        label: "消息草稿保存失败",
        kind: "unsaved",
        active: true,
      });
      return;
    }
    persistedTextRef.current = draft.text;
    clearReloadBlocker("composer-draft");
  }, []);

  const setValue = useCallback((next: string) => {
    draftRef.current = { ...draftRef.current, text: next };
    setValueState(next);
    if (!next) {
      if (clearComposerDraft({ conversationId: draftRef.current.conversationId, projectId: draftRef.current.projectId })) {
        persistedTextRef.current = "";
        clearReloadBlocker("composer-draft");
      } else {
        setReloadBlocker({
          id: "composer-draft",
          label: "消息草稿清理失败",
          kind: "unsaved",
          active: true,
        });
      }
      return;
    }
    setReloadBlocker({
      id: "composer-draft",
      label: "消息草稿正在保存",
      kind: "unsaved",
      active: next !== persistedTextRef.current,
    });
  }, []);

  useEffect(() => {
    const current = draftRef.current;
    if (current.conversationId === conversationId && current.projectId === projectId) return;
    flushDraft();
    const restored = loadComposerDraft({ conversationId, projectId })?.text ?? "";
    draftRef.current = { conversationId, projectId, text: restored };
    persistedTextRef.current = restored;
    setValueState(restored);
  }, [conversationId, flushDraft, projectId]);

  useEffect(() => {
    if (value === persistedTextRef.current) return;
    const timer = window.setTimeout(flushDraft, COMPOSER_SAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [flushDraft, value]);

  useEffect(() => {
    const unregister = registerReloadFlusher("composer-draft", flushDraft);
    return () => {
      flushDraft();
      unregister();
      clearReloadBlocker("composer-draft");
    };
  }, [flushDraft]);

  function submit() {
    const content = value.trim();
    if (chat.state.requestStatus === "streaming") return;
    if (!online) {
      chat.notify("当前处于离线模式，不能发送消息");
      return;
    }
    if (attachments.state.uploading) {
      chat.notify("文件还在上传或识别，请稍等");
      return;
    }
    if (attachments.hasErrors) {
      chat.notify("请先移除识别失败的文件");
      return;
    }
    const ready = attachments.consumeReadyAttachments();
    if (!content && !ready.length && !chat.quoteDraft) return;
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

  const canSend = Boolean(value.trim()) || attachments.readyCount > 0 || Boolean(chat.quoteDraft);

  return { value, setValue, flushDraft, onSubmit, onKeyDown, submit, canSend };
}
