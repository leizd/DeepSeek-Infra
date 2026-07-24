import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { generateConversationTitle } from "../../api/titleApi";
import { streamChat } from "../../api/chatStream";
import { normalizeMemorySuggestion } from "../../api/memoryApi";
import { createReminder } from "../../api/remindersApi";
import { chatReducer, createInitialChatState } from "../../domain/chat/chatReducer";
import {
  applyProjectContext,
  buildChatPayload,
  buildContinuationPayload,
  buildRegenerationPayload,
  type ChatRequestSettings,
} from "../../domain/chat/requestBuilder";
import { selectCurrentMessages } from "../../domain/chat/selectors";
import { applyStreamEvent, createAssistantMessage, resetAssistantMessage } from "../../domain/chat/streamReducer";
import type { Attachment, ChatMessage, ChatRequestPayload, QuoteDraft } from "../../domain/chat/types";
import { loadPersistedConversationState, savePersistedConversationState } from "../../domain/conversation/persistence";
import { createId } from "../../shared/createId";
import { useMemory } from "../../contexts/MemoryContext";
import { useSettings } from "../../contexts/SettingsContext";
import { useProjects } from "../../contexts/ProjectsContext";
import { createOutputPauseGate } from "../activity/outputPause";
import { useAgentRun } from "../agent-run/useAgentRun";
import { detectReminderFromText } from "../reminders/reminderParse";
import { ensureNotificationPermission } from "../reminders/useReminderPolling";
import { quoteAwareContent } from "./messageActions";

function userMessage(content: string, attachments: readonly Attachment[]): ChatMessage {
  return {
    id: createId("user"),
    role: "user",
    content,
    reasoning: "",
    createdAt: Date.now(),
    phase: "done",
    streaming: false,
    attachments,
    timeline: [],
    systemNotes: [],
  };
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error && reason.message ? reason.message : "请求失败，请重试";
}

function isAbortError(reason: unknown): boolean {
  return reason instanceof DOMException
    ? reason.name === "AbortError"
    : reason instanceof Error && reason.name === "AbortError";
}

export interface PendingMemorySuggestion {
  id: string;
  content: string;
  category: string;
  scope: string;
  conflicts: readonly { id: string; content: string; reason: string }[];
}

export function useChatController() {
  const settings = useSettings();
  const projects = useProjects();
  const memory = useMemory();
  const [state, dispatch] = useReducer(
    chatReducer,
    undefined,
    () => createInitialChatState(loadPersistedConversationState()),
  );
  const stateRef = useRef(state);
  stateRef.current = state;
  const abortControllerRef = useRef<AbortController | null>(null);
  const outputPauseGateRef = useRef<ReturnType<typeof createOutputPauseGate> | null>(null);
  if (!outputPauseGateRef.current) outputPauseGateRef.current = createOutputPauseGate();
  const [outputPaused, setOutputPaused] = useState(false);
  const [pendingMemorySuggestion, setPendingMemorySuggestion] = useState<PendingMemorySuggestion | null>(null);
  const [quoteDraft, setQuoteDraft] = useState<QuoteDraft | null>(null);
  const waitUntilResumed = useCallback(() => outputPauseGateRef.current?.waitUntilResumed() ?? Promise.resolve(), []);

  const flushConversationPersistence = useCallback(() => {
    const current = stateRef.current;
    savePersistedConversationState({
      schemaVersion: 1,
      currentConversationId: current.currentConversationId,
      conversations: current.conversations,
    });
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(flushConversationPersistence, 120);
    return () => window.clearTimeout(timer);
  }, [flushConversationPersistence, state.conversations, state.currentConversationId]);

  const requestSettings = useCallback((): ChatRequestSettings => ({
    apiKey: settings.apiKey,
    tavilyApiKey: settings.tavilyApiKey,
    model: settings.model,
    thinkingEnabled: settings.thinkingEnabled,
    searchEnabled: settings.searchEnabled,
    memoryEnabled: settings.memoryEnabled,
  }), [settings]);

  const streamIntoMessage = useCallback(
    async (assistantMessage: ChatMessage, payload: ChatRequestPayload): Promise<ChatMessage | null> => {
      const controller = new AbortController();
      abortControllerRef.current = controller;
      let current = assistantMessage;
      let terminalReceived = false;
      try {
        for await (const event of streamChat(payload, { signal: controller.signal, waitUntilResumed })) {
          current = applyStreamEvent(current, event);
          dispatch({ type: "streamEventReceived", messageId: current.id, event });
          if (event.type === "done" || event.type === "error") terminalReceived = true;
          if (event.type === "memory_suggestion") {
            const suggestion = normalizeMemorySuggestion(event.payload);
            if (suggestion) {
              setPendingMemorySuggestion({
                id: createId("memory-suggestion"),
                ...suggestion,
                conflicts: [],
              });
            }
          }
        }
        if (!terminalReceived) {
          dispatch({ type: "requestFailed", messageId: current.id, error: "连接提前结束，请重试" });
          return null;
        }
        return current;
      } catch (reason) {
        if (controller.signal.aborted || isAbortError(reason)) {
          dispatch({ type: "requestStopped", messageId: current.id });
        } else {
          dispatch({ type: "requestFailed", messageId: current.id, error: errorMessage(reason) });
        }
        return null;
      } finally {
        if (abortControllerRef.current === controller) abortControllerRef.current = null;
      }
    },
    [waitUntilResumed],
  );

  const maybeGenerateTitle = useCallback(
    async (conversationId: string, firstTurn: boolean, userText: string, assistantContent: string) => {
      if (!firstTurn || !assistantContent.trim()) return;
      try {
        const title = await generateConversationTitle({
          apiKey: settings.apiKey,
          userMessage: userText,
          assistantMessage: assistantContent,
        });
        if (title) dispatch({ type: "conversationTitleUpdated", conversationId, title });
      } catch {
        // Local title remains available when best-effort title generation fails.
      }
    },
    [settings.apiKey],
  );

  const hasBackendKey = useCallback(
    () => Boolean(settings.apiKey.trim() || settings.runtime?.hasServerKey),
    [settings.apiKey, settings.runtime],
  );

  const maybeCreateReminder = useCallback((input: string) => {
    const draft = detectReminderFromText(input);
    if (!draft) return;
    void createReminder(draft)
      .then(() => {
        dispatch({ type: "noticeSet", notice: "已创建本地提醒" });
        void ensureNotificationPermission();
      })
      .catch(() => undefined);
  }, []);

  const agentRun = useAgentRun({
    state,
    dispatch,
    abortControllerRef,
    requestSettings,
    hasBackendKey,
    maybeGenerateTitle,
    waitUntilResumed,
  });

  const sendMessage = useCallback(
    async (input: string, options: { attachments?: readonly Attachment[] } = {}) => {
      if (settings.agentMode) {
        await agentRun.sendAgentMessage(input, options);
        return;
      }
      const quotedContent = quoteAwareContent(input.trim(), quoteDraft);
      const attachments = options.attachments ?? [];
      if ((!quotedContent && !attachments.length) || state.requestStatus === "streaming") return;
      if (!hasBackendKey()) {
        dispatch({ type: "noticeSet", notice: "请先在连接设置中输入 DeepSeek API Key" });
        return;
      }

      const existingMessages = selectCurrentMessages(state);
      const projectContext = projects.chatContext();
      const newUserMessage = applyProjectContext(userMessage(quotedContent, attachments), projectContext);
      setQuoteDraft(null);
      maybeCreateReminder(input.trim());
      const assistantMessage = createAssistantMessage(createId("assistant"));
      const conversationId = state.currentConversationId ?? createId("conversation");
      const firstTurn = !existingMessages.some((message) => message.role === "user");
      const payload = buildChatPayload(existingMessages, newUserMessage, requestSettings(), {
        memoryScope: projectContext.memoryScope,
      });

      dispatch({
        type: "requestStarted",
        conversationId,
        userMessage: newUserMessage,
        assistantMessage,
        model: settings.model,
        thinkingEnabled: settings.thinkingEnabled,
      });

      const finished = await streamIntoMessage(assistantMessage, payload);
      await maybeGenerateTitle(conversationId, firstTurn, newUserMessage.content, finished?.content ?? "");
    },
    [agentRun, hasBackendKey, maybeCreateReminder, maybeGenerateTitle, projects, quoteDraft, requestSettings, settings, state, streamIntoMessage],
  );

  const editAndResend = useCallback(
    async (messageId: string, input: string) => {
      const content = input.trim();
      if (state.requestStatus === "streaming" || !state.currentConversationId) return;
      const messages = selectCurrentMessages(state);
      const target = messages.find((message) => message.id === messageId && message.role === "user");
      if (!target) return;
      if (!content && !target.attachments.length) {
        dispatch({ type: "noticeSet", notice: "请输入修改后的内容" });
        return;
      }
      if (!hasBackendKey()) {
        dispatch({ type: "noticeSet", notice: "请先在连接设置中输入 DeepSeek API Key" });
        return;
      }

      const targetIndex = messages.findIndex((message) => message.id === messageId);
      const editedUserMessage: ChatMessage = { ...target, content, updatedAt: Date.now() };
      const assistantMessage = createAssistantMessage(createId("assistant"));
      const payload = buildChatPayload(messages.slice(0, targetIndex), editedUserMessage, requestSettings());

      dispatch({
        type: "messageEditResubmitted",
        messageId,
        content,
        updatedAt: editedUserMessage.updatedAt as number,
        assistantMessage,
        model: settings.model,
        thinkingEnabled: settings.thinkingEnabled,
      });

      const firstTurn = !messages.slice(0, targetIndex).some((message) => message.role === "user");
      const finished = await streamIntoMessage(assistantMessage, payload);
      await maybeGenerateTitle(state.currentConversationId, firstTurn, content, finished?.content ?? "");
    },
    [hasBackendKey, maybeGenerateTitle, requestSettings, settings, state, streamIntoMessage],
  );

  const regenerate = useCallback(
    async (messageId: string) => {
      if (state.requestStatus === "streaming") return;
      const messages = selectCurrentMessages(state);
      const targetIndex = messages.findIndex((message) => message.id === messageId && message.role === "assistant");
      if (targetIndex <= 0) return;
      const messagesBefore = messages.slice(0, targetIndex);
      if (!messagesBefore.some((message) => message.role === "user")) {
        dispatch({ type: "noticeSet", notice: "没有可重新生成的用户问题" });
        return;
      }
      if (!hasBackendKey()) {
        dispatch({ type: "noticeSet", notice: "请先在连接设置中输入 DeepSeek API Key" });
        return;
      }

      const payload = buildRegenerationPayload(messagesBefore, requestSettings());
      dispatch({ type: "assistantRegenerated", messageId });
      await streamIntoMessage(resetAssistantMessage(messages[targetIndex]), payload);
    },
    [hasBackendKey, requestSettings, state, streamIntoMessage],
  );

  const continueGeneration = useCallback(
    async (messageId: string) => {
      if (state.requestStatus === "streaming") return;
      const messages = selectCurrentMessages(state);
      const targetIndex = messages.findIndex((message) => message.id === messageId && message.role === "assistant");
      if (targetIndex < 0) return;
      const target = messages[targetIndex];
      if (!target.interrupted) return;
      if (!hasBackendKey()) {
        dispatch({ type: "noticeSet", notice: "请先在连接设置中输入 DeepSeek API Key" });
        return;
      }

      const payload = buildContinuationPayload(messages.slice(0, targetIndex), target, requestSettings());
      dispatch({ type: "continuationStarted", messageId });
      await streamIntoMessage(target, payload);
    },
    [hasBackendKey, requestSettings, state, streamIntoMessage],
  );

  const stopGeneration = useCallback(() => abortControllerRef.current?.abort(), []);

  const pauseOutput = useCallback(() => {
    outputPauseGateRef.current?.pause();
    setOutputPaused(true);
  }, []);

  const resumeOutput = useCallback(() => {
    outputPauseGateRef.current?.resume();
    setOutputPaused(false);
  }, []);

  useEffect(() => {
    if (state.requestStatus === "idle" && outputPaused) resumeOutput();
  }, [state.requestStatus, outputPaused, resumeOutput]);

  const saveMemorySuggestion = useCallback(
    async (replaceIds: readonly string[] = []) => {
      const suggestion = pendingMemorySuggestion;
      if (!suggestion) return;
      try {
        const result = await memory.save({
          content: suggestion.content,
          category: suggestion.category,
          scope: suggestion.scope,
          replaceIds,
        });
        if (!result.saved) {
          setPendingMemorySuggestion((current) => current?.id === suggestion.id
            ? { ...current, conflicts: result.conflicts }
            : current,
          );
          return;
        }
        setPendingMemorySuggestion((current) => current?.id === suggestion.id ? null : current);
        dispatch({ type: "noticeSet", notice: "已保存到长期记忆" });
      } catch (reason) {
        dispatch({ type: "noticeSet", notice: errorMessage(reason) });
      }
    },
    [memory, pendingMemorySuggestion],
  );

  const dismissMemorySuggestion = useCallback(() => setPendingMemorySuggestion(null), []);

  const quoteMessage = useCallback((message: ChatMessage, fragment?: string) => {
    const text = (fragment ?? message.content).trim();
    if (!text) return;
    setQuoteDraft({
      messageId: message.id,
      role: message.role,
      text: message.content.trim(),
      fragment: text,
      isFragment: Boolean(fragment && fragment.trim() !== message.content.trim()),
    });
  }, []);

  const clearQuote = useCallback(() => setQuoteDraft(null), []);

  useEffect(() => {
    setQuoteDraft(null);
  }, [state.currentConversationId]);

  return {
    state,
    messages: selectCurrentMessages(state),
    outputPaused,
    pendingMemorySuggestion,
    quoteDraft,
    sendMessage,
    editAndResend,
    regenerate,
    continueGeneration,
    confirmAgentPlan: agentRun.confirmPlan,
    rerunAgentPhase: agentRun.rerunPhase,
    stopGeneration,
    pauseOutput,
    resumeOutput,
    newConversation: () => dispatch({ type: "newConversation" }),
    openConversation: (conversationId: string) => dispatch({ type: "openConversation", conversationId }),
    deleteConversation: (conversationId: string) => dispatch({ type: "deleteConversation", conversationId }),
    renameConversation: (conversationId: string, title: string) =>
      dispatch({ type: "conversationRenamed", conversationId, title, updatedAt: Date.now() }),
    toggleFavorite: (conversationId: string) => dispatch({ type: "conversationFavoriteToggled", conversationId, updatedAt: Date.now() }),
    clearNotice: () => dispatch({ type: "noticeCleared" }),
    notify: (notice: string) => dispatch({ type: "noticeSet", notice }),
    saveMemorySuggestion,
    dismissMemorySuggestion,
    quoteMessage,
    clearQuote,
    flushConversationPersistence,
  };
}
