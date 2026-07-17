import { useCallback, useEffect, useReducer, useRef } from "react";

import { generateConversationTitle } from "../../api/titleApi";
import { streamChat } from "../../api/chatStream";
import { chatReducer, createInitialChatState } from "../../domain/chat/chatReducer";
import { buildChatPayload } from "../../domain/chat/requestBuilder";
import { selectCurrentMessages } from "../../domain/chat/selectors";
import { applyStreamEvent, createAssistantMessage } from "../../domain/chat/streamReducer";
import type { ChatMessage } from "../../domain/chat/types";
import { loadPersistedConversationState, savePersistedConversationState } from "../../domain/conversation/persistence";
import { createId } from "../../shared/createId";
import { useSettings } from "../../contexts/SettingsContext";

function userMessage(content: string): ChatMessage {
  return {
    id: createId("user"),
    role: "user",
    content,
    reasoning: "",
    createdAt: Date.now(),
    phase: "done",
    streaming: false,
    attachments: [],
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

export function useChatController() {
  const settings = useSettings();
  const [state, dispatch] = useReducer(
    chatReducer,
    undefined,
    () => createInitialChatState(loadPersistedConversationState()),
  );
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      savePersistedConversationState({
        schemaVersion: 1,
        currentConversationId: state.currentConversationId,
        conversations: state.conversations,
      });
    }, 120);
    return () => window.clearTimeout(timer);
  }, [state.conversations, state.currentConversationId]);

  const sendMessage = useCallback(
    async (input: string) => {
      const content = input.trim();
      if (!content || state.requestStatus === "streaming") return;
      if (!settings.apiKey.trim() && !settings.runtime?.hasServerKey) {
        dispatch({ type: "noticeSet", notice: "请先在连接设置中输入 DeepSeek API Key" });
        return;
      }

      const existingMessages = selectCurrentMessages(state);
      const newUserMessage = userMessage(content);
      let assistantMessage = createAssistantMessage(createId("assistant"));
      const conversationId = state.currentConversationId ?? createId("conversation");
      const firstTurn = !existingMessages.some((message) => message.role === "user");
      const payload = buildChatPayload(existingMessages, newUserMessage, {
        apiKey: settings.apiKey,
        tavilyApiKey: settings.tavilyApiKey,
        model: settings.model,
        thinkingEnabled: settings.thinkingEnabled,
        searchEnabled: settings.searchEnabled,
      });

      dispatch({
        type: "requestStarted",
        conversationId,
        userMessage: newUserMessage,
        assistantMessage,
        model: settings.model,
        thinkingEnabled: settings.thinkingEnabled,
      });

      const controller = new AbortController();
      abortControllerRef.current = controller;
      let terminalReceived = false;
      try {
        for await (const event of streamChat(payload, { signal: controller.signal })) {
          assistantMessage = applyStreamEvent(assistantMessage, event);
          dispatch({ type: "streamEventReceived", messageId: assistantMessage.id, event });
          if (event.type === "done" || event.type === "error") terminalReceived = true;
        }
        if (!terminalReceived) {
          dispatch({ type: "requestFailed", messageId: assistantMessage.id, error: "连接提前结束，请重试" });
          return;
        }
        if (firstTurn && assistantMessage.content.trim()) {
          try {
            const title = await generateConversationTitle({
              apiKey: settings.apiKey,
              userMessage: newUserMessage.content,
              assistantMessage: assistantMessage.content,
            });
            if (title) dispatch({ type: "conversationTitleUpdated", conversationId, title });
          } catch {
            // Local title remains available when best-effort title generation fails.
          }
        }
      } catch (reason) {
        if (controller.signal.aborted || isAbortError(reason)) {
          dispatch({ type: "requestStopped", messageId: assistantMessage.id });
        } else {
          dispatch({ type: "requestFailed", messageId: assistantMessage.id, error: errorMessage(reason) });
        }
      } finally {
        if (abortControllerRef.current === controller) abortControllerRef.current = null;
      }
    },
    [settings, state],
  );

  const stopGeneration = useCallback(() => abortControllerRef.current?.abort(), []);

  return {
    state,
    messages: selectCurrentMessages(state),
    sendMessage,
    stopGeneration,
    newConversation: () => dispatch({ type: "newConversation" }),
    openConversation: (conversationId: string) => dispatch({ type: "openConversation", conversationId }),
    deleteConversation: (conversationId: string) => dispatch({ type: "deleteConversation", conversationId }),
    clearNotice: () => dispatch({ type: "noticeCleared" }),
  };
}
