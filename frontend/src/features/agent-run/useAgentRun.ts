import { useCallback, useEffect, useRef, type Dispatch, type MutableRefObject } from "react";

import {
  confirmAgentPlan,
  createAgentRun,
  getAgentRun,
  isActiveRunStatus,
  rerunAgentPhase,
  resumeAgentRun,
  type AgentPlanItem,
  type AgentRun,
} from "../../api/agentRunApi";
import { ApiError } from "../../api/httpClient";
import { createAssistantMessage } from "../../domain/chat/streamReducer";
import { applyProjectContext, buildChatPayload, type ChatRequestSettings } from "../../domain/chat/requestBuilder";
import { selectCurrentMessages } from "../../domain/chat/selectors";
import type { ChatAction, ChatState } from "../../domain/chat/chatReducer";
import type { Attachment, ChatMessage, JsonRecord } from "../../domain/chat/types";
import { createId } from "../../shared/createId";
import { useSettings } from "../../contexts/SettingsContext";
import { useProjects } from "../../contexts/ProjectsContext";
import { streamAgentRunEvents } from "./agentRunFlow";

function errorMessage(reason: unknown): string {
  return reason instanceof Error && reason.message ? reason.message : "请求失败，请重试";
}

function isAbortError(reason: unknown): boolean {
  return reason instanceof Error && reason.name === "AbortError";
}

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

export interface AgentRunHookParams {
  state: ChatState;
  dispatch: Dispatch<ChatAction>;
  abortControllerRef: MutableRefObject<AbortController | null>;
  requestSettings(): ChatRequestSettings;
  hasBackendKey(): boolean;
  maybeGenerateTitle(conversationId: string, firstTurn: boolean, userText: string, assistantContent: string): Promise<void>;
  waitUntilResumed?: () => Promise<void>;
}

export function useAgentRun(params: AgentRunHookParams) {
  const settings = useSettings();
  const projects = useProjects();
  const { state, dispatch, abortControllerRef, requestSettings, hasBackendKey, maybeGenerateTitle, waitUntilResumed } = params;

  const runtimePayload = useCallback((): JsonRecord => {
    const base = requestSettings();
    return {
      ...(base.apiKey.trim() ? { apiKey: base.apiKey.trim() } : {}),
      ...(base.searchEnabled && base.tavilyApiKey.trim() ? { tavilyApiKey: base.tavilyApiKey.trim() } : {}),
      model: base.model,
      thinkingEnabled: base.thinkingEnabled,
      searchEnabled: base.searchEnabled,
    };
  }, [requestSettings]);

  const attachStream = useCallback(
    async (assistantMessage: ChatMessage, runId: string, after: number, onFailure?: (reason: unknown) => void): Promise<string> => {
      const controller = new AbortController();
      abortControllerRef.current = controller;
      let lastStatus = assistantMessage.agentRunStatus ?? "created";
      let content = assistantMessage.content === "Agent 计划已生成，等待确认执行。" ? "" : assistantMessage.content;
      let settled = false;
      try {
        await streamAgentRunEvents({
          runId,
          after,
          signal: controller.signal,
          waitUntilResumed,
          onEvent: (event) => {
            if (event.type === "run_status") lastStatus = event.status;
            if (event.type === "done") {
              lastStatus = "done";
              content = event.content ?? content;
            }
            if (event.type === "error") lastStatus = "failed";
            if (event.type === "content") content += event.text;
            if (event.type === "final_reset" && event.scope === "final_answer") content = "";
            dispatch({ type: "streamEventReceived", messageId: assistantMessage.id, event });
          },
          isComplete: () => !isActiveRunStatus(lastStatus),
        });
        settled = true;
      } catch (reason) {
        if (controller.signal.aborted || isAbortError(reason)) {
          dispatch({ type: "requestStopped", messageId: assistantMessage.id });
        } else if (onFailure) {
          onFailure(reason);
        } else {
          dispatch({ type: "requestFailed", messageId: assistantMessage.id, error: errorMessage(reason) });
        }
      } finally {
        if (settled) dispatch({ type: "agentStreamSettled", messageId: assistantMessage.id });
        if (abortControllerRef.current === controller) abortControllerRef.current = null;
      }
      return content;
    },
    [abortControllerRef, dispatch, waitUntilResumed],
  );

  const sendAgentMessage = useCallback(
    async (input: string, options: { attachments?: readonly Attachment[] } = {}) => {
      const content = input.trim();
      const attachments = options.attachments ?? [];
      if ((!content && !attachments.length) || state.requestStatus === "streaming") return;
      if (!hasBackendKey()) {
        dispatch({ type: "noticeSet", notice: "请先在连接设置中输入 DeepSeek API Key" });
        return;
      }

      const existingMessages = selectCurrentMessages(state);
      const projectContext = projects.chatContext();
      const newUserMessage = applyProjectContext(userMessage(content, attachments), projectContext);
      const assistantMessage = createAssistantMessage(createId("assistant"));
      const conversationId = state.currentConversationId ?? createId("conversation");
      const firstTurn = !existingMessages.some((message) => message.role === "user");
      const payload = {
        ...buildChatPayload(existingMessages, newUserMessage, requestSettings(), {
          memoryScope: projectContext.memoryScope,
        }),
        agentMode: true,
      };

      dispatch({
        type: "requestStarted",
        conversationId,
        userMessage: newUserMessage,
        assistantMessage,
        model: settings.model,
        thinkingEnabled: settings.thinkingEnabled,
      });

      let runId = "";
      try {
        const created = await createAgentRun({
          payload,
          confirmPlan: settings.agentPreset === "plan",
          agentPreset: settings.agentPreset === "auto" ? "auto" : "full",
          conversationId,
          messageId: assistantMessage.id,
        });
        runId = created.runId;
        dispatch({
          type: "streamEventReceived",
          messageId: assistantMessage.id,
          event: { type: "run_status", status: created.run.status, runId },
        });
      } catch (reason) {
        dispatch({ type: "requestFailed", messageId: assistantMessage.id, error: errorMessage(reason) });
        return;
      }

      const streamedContent = await attachStream({ ...assistantMessage, agentRunId: runId }, runId, -1);
      await maybeGenerateTitle(conversationId, firstTurn, newUserMessage.content, streamedContent);
    },
    [attachStream, dispatch, hasBackendKey, maybeGenerateTitle, projects, requestSettings, settings, state],
  );

  const confirmPlan = useCallback(
    async (message: ChatMessage, plan: AgentPlanItem[]) => {
      if (!message.agentRunId || state.requestStatus === "streaming") return;
      dispatch({ type: "agentStreamAttached", messageId: message.id });
      try {
        await confirmAgentPlan(message.agentRunId, { payload: runtimePayload(), plan });
      } catch (reason) {
        dispatch({ type: "requestFailed", messageId: message.id, error: errorMessage(reason) });
        return;
      }
      await attachStream(message, message.agentRunId, message.agentRunLastEventIndex ?? -1);
    },
    [attachStream, dispatch, runtimePayload, state.requestStatus],
  );

  const rerunPhase = useCallback(
    async (message: ChatMessage, phase: string) => {
      if (!message.agentRunId || state.requestStatus === "streaming") return;
      dispatch({ type: "agentStreamAttached", messageId: message.id });
      try {
        await rerunAgentPhase(message.agentRunId, { payload: runtimePayload(), agentId: phase, resynthesize: true });
      } catch (reason) {
        dispatch({ type: "requestFailed", messageId: message.id, error: errorMessage(reason) });
        return;
      }
      await attachStream(message, message.agentRunId, message.agentRunLastEventIndex ?? -1);
    },
    [attachStream, dispatch, runtimePayload, state.requestStatus],
  );

  const reconcileRestoredRun = useCallback(
    async (conversationId: string, candidate: ChatMessage): Promise<void> => {
      const runId = candidate.agentRunId;
      if (!runId) return;
      dispatch({ type: "openConversation", conversationId });
      // 只读对账：先问服务器 run 的真实状态，绝不新建 run、绝不重放已付 token。
      let run: AgentRun;
      try {
        run = await getAgentRun(runId);
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 404) {
          dispatch({ type: "agentRunMissing", messageId: candidate.id });
        } else {
          dispatch({ type: "agentRunOrphaned", messageId: candidate.id });
        }
        return;
      }
      if (isActiveRunStatus(run.status)) {
        dispatch({ type: "agentStreamAttached", messageId: candidate.id });
        void attachStream(candidate, runId, candidate.agentRunLastEventIndex ?? -1, () => {
          dispatch({ type: "agentRunOrphaned", messageId: candidate.id });
        });
        return;
      }
      // 终态：只把服务器结论映射到消息上，不重新执行任何内容。
      if (run.status === "done") {
        dispatch({
          type: "streamEventReceived",
          messageId: candidate.id,
          event: { type: "done", content: run.finalAnswer || undefined },
        });
        return;
      }
      if (run.status === "failed") {
        const detail = typeof run.diagnostics.error === "string" ? run.diagnostics.error.trim() : "";
        dispatch({
          type: "streamEventReceived",
          messageId: candidate.id,
          event: { type: "error", error: detail || "Agent Run 已失败" },
        });
        return;
      }
      dispatch({
        type: "streamEventReceived",
        messageId: candidate.id,
        event: { type: "run_status", status: run.status, runId },
      });
    },
    [attachStream, dispatch],
  );

  const resumedRef = useRef(false);
  useEffect(() => {
    if (resumedRef.current) return;
    resumedRef.current = true;
    if (state.requestStatus === "streaming") return;
    for (const conversation of state.conversations) {
      const candidate = [...conversation.messages]
        .reverse()
        .find(
          (message) =>
            message.role === "assistant" && message.agentRunId && isActiveRunStatus(message.agentRunStatus),
        );
      if (candidate?.agentRunId) {
        void reconcileRestoredRun(conversation.id, candidate);
        return;
      }
    }
  }, [dispatch, reconcileRestoredRun, state.conversations, state.requestStatus]);

  const resumeRun = useCallback(
    async (messageId: string): Promise<void> => {
      if (state.requestStatus === "streaming") return;
      const message = selectCurrentMessages(state).find((item) => item.id === messageId && item.role === "assistant");
      const runId = message?.agentRunId;
      if (!message || !runId || message.agentRunStatus !== "orphaned") return;
      // 先只读确认服务器状态：run 仍活跃时直接重连流，只有非活跃才 POST resume，
      // 避免对仍在运行的 run 触发 409；任何路径都不新建 run。
      let run: AgentRun;
      try {
        run = await getAgentRun(runId);
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 404) {
          dispatch({ type: "agentRunMissing", messageId });
        } else {
          dispatch({ type: "noticeSet", notice: errorMessage(reason) });
        }
        return;
      }
      if (!isActiveRunStatus(run.status)) {
        try {
          await resumeAgentRun(runId, { payload: runtimePayload() });
        } catch (reason) {
          // 保持 orphaned 状态，"恢复 Agent Run"按钮可再次尝试。
          dispatch({ type: "noticeSet", notice: errorMessage(reason) });
          return;
        }
      }
      dispatch({ type: "agentStreamAttached", messageId });
      void attachStream(message, runId, message.agentRunLastEventIndex ?? -1, () => {
        dispatch({ type: "agentRunOrphaned", messageId });
      });
    },
    [attachStream, dispatch, runtimePayload, state],
  );

  return { sendAgentMessage, confirmPlan, rerunPhase, attachStream, resumeRun };
}
