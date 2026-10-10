import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import {
  clearReloadBlocker,
  registerReloadFlusher,
  setReloadBlocker,
  type PersistenceFlushResult,
} from "../../app/reloadBlockers";
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
  type ComposerDraftScope,
} from "./composerDraftPersistence";
import { forgetDraft, markPersisted, recallDraft, rememberDraft } from "./composerDraftRepository";

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
  const [initialDraft] = useState((): { text: string; persisted: string | null } => {
    const scope: ComposerDraftScope = { conversationId, projectId };
    // 内存仓库优先：上次写存储失败留下的 dirty 文本也能恢复。
    const memory = recallDraft(scope);
    if (memory) {
      return { text: memory.text, persisted: memory.dirty ? null : memory.text };
    }
    const stored = loadComposerDraft(scope)?.text ?? "";
    return { text: stored, persisted: stored };
  });
  const [value, setValueState] = useState(initialDraft.text);
  const draftRef = useRef({ conversationId, projectId, text: initialDraft.text });
  // null 表示“当前文本尚未确认落盘”，迫使防抖保存再试一次。
  const persistedTextRef = useRef<string | null>(initialDraft.persisted);

  // 向指定作用域写存储：成功时同步内存仓库的落盘状态；失败时内存原样保留。
  const persistScope = useCallback((scope: ComposerDraftScope, text: string): PersistenceFlushResult => {
    const updatedAt = Date.now();
    const result = text
      ? saveComposerDraft({
        conversationId: scope.conversationId,
        projectId: scope.projectId,
        text,
        updatedAt,
      })
      : clearComposerDraft(scope);
    if (!result.ok) return result;
    const entry = recallDraft(scope);
    if (text) {
      // 内存里的文本没有更新过才标记已落盘，避免更旧的定时器覆盖新输入。
      if (entry && entry.text === text) markPersisted(scope, result.revision ?? String(updatedAt));
    } else if (!entry || entry.text === "") {
      forgetDraft(scope);
    }
    return result;
  }, []);

  const flushDraft = useCallback((): PersistenceFlushResult => {
    const draft = draftRef.current;
    const result = persistScope(
      { conversationId: draft.conversationId, projectId: draft.projectId },
      draft.text,
    );
    if (!result.ok) {
      setReloadBlocker({
        id: "composer-draft",
        label: "消息草稿保存失败",
        kind: "unsaved",
        active: true,
      });
      return result;
    }
    persistedTextRef.current = draft.text;
    clearReloadBlocker("composer-draft");
    return result;
  }, [persistScope]);

  const setValue = useCallback((next: string) => {
    const scope: ComposerDraftScope = {
      conversationId: draftRef.current.conversationId,
      projectId: draftRef.current.projectId,
    };
    // 任何输入先同步落内存仓库，再碰存储——存储失败也不丢文本。
    rememberDraft(scope, { text: next });
    draftRef.current = { ...draftRef.current, text: next };
    setValueState(next);
    if (!next) {
      const result = persistScope(scope, "");
      if (result.ok) {
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
  }, [persistScope]);

  useEffect(() => {
    const current = draftRef.current;
    if (current.conversationId === conversationId && current.projectId === projectId) return;
    // 旧作用域先刷盘；即使写存储失败，文本仍以 dirty 状态留在内存仓库里。
    flushDraft();
    const nextScope: ComposerDraftScope = { conversationId, projectId };
    const memory = recallDraft(nextScope);
    const restored = memory?.text ?? loadComposerDraft(nextScope)?.text ?? "";
    draftRef.current = { conversationId, projectId, text: restored };
    persistedTextRef.current = memory?.dirty ? null : restored;
    setValueState(restored);
  }, [conversationId, flushDraft, projectId]);

  useEffect(() => {
    if (value === persistedTextRef.current) return;
    // 捕获调度时刻的作用域与文本：过期的定时器只会写它自己那个作用域的键，
    // 永远碰不到切换后的新作用域。
    const scope: ComposerDraftScope = {
      conversationId: draftRef.current.conversationId,
      projectId: draftRef.current.projectId,
    };
    const text = value;
    const timer = window.setTimeout(() => {
      const result = persistScope(scope, text);
      const current = draftRef.current;
      const isCurrentScope = current.conversationId === scope.conversationId && current.projectId === scope.projectId;
      if (!result.ok) {
        setReloadBlocker({
          id: "composer-draft",
          label: "消息草稿保存失败",
          kind: "unsaved",
          active: true,
        });
        return;
      }
      // 只有当前作用域且文本未再变化时才推进已落盘基线并解除 blocker。
      if (isCurrentScope && current.text === text) {
        persistedTextRef.current = text;
        clearReloadBlocker("composer-draft");
      }
    }, COMPOSER_SAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [persistScope, value]);

  useEffect(() => {
    const unregister = registerReloadFlusher("composer-draft", flushDraft, { failureLabel: "草稿保存失败" });
    return () => {
      flushDraft();
      unregister();
      clearReloadBlocker("composer-draft");
    };
  }, [flushDraft]);

  function submit() {
    const content = value.trim();
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
    // 缺 Key 只打开设置页，不触碰消息与附件，文本和附件原样保留。
    if (!settings.apiKey.trim() && !settings.runtime?.hasServerKey) {
      overlay.openOverlay("settings");
      return;
    }
    // 先只读快照，确认消息被接受后才提交消费附件、清空草稿。
    const ready = attachments.peekReadyAttachments();
    const result = chat.tryStartMessage(content, {
      attachments: ready.map((entry) => entry.attachment),
      online,
    });
    if (!result.accepted) {
      if (result.reason === "missing-key") overlay.openOverlay("settings");
      return;
    }
    attachments.commitReadyAttachments(ready.map((entry) => entry.id));
    setValue("");
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
