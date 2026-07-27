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
import {
  conversationStorageKeys,
  createConversationPersistenceAdapter,
  type ConversationConflictSignal,
  type ConversationPersistenceAdapter,
  type SaveConversationOptions,
  type StorageLike,
} from "../../domain/conversation/persistence";
import type { PersistedConversationState } from "../../domain/conversation/types";
import { recordFlushReport } from "../../app/persistenceHealth";
import type { PersistenceFlushResult } from "../../app/reloadBlockers";
import type { PersistenceFlushFailure } from "../../app/persistenceErrors";
import { createConversationSyncChannel, type ConversationSyncChannel } from "../../app/conversationSync";
import { createId } from "../../shared/createId";
import { useMemory } from "../../contexts/MemoryContext";
import { useSettings } from "../../contexts/SettingsContext";
import { useProjects } from "../../contexts/ProjectsContext";
import { createOutputPauseGate } from "../activity/outputPause";
import { useAgentRun } from "../agent-run/useAgentRun";
import { detectReminderFromText } from "../reminders/reminderParse";
import { ensureNotificationPermission } from "../reminders/useReminderPolling";
import { decideCheckpointDelay } from "./checkpointSchedule";
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

/** flush 异常统一映射为 unknown 失败结果（防抖提交与生命周期 flush 共用）。 */
function flushFailureResult(reason: unknown): PersistenceFlushFailure {
  return {
    ok: false,
    code: "unknown",
    message: reason instanceof Error && reason.message ? reason.message : "对话记录保存失败",
  };
}

const STORAGE_SYNC_FALLBACK_MS = 1_000;

export type MessageSubmissionResult =
  | { accepted: true; conversationId: string }
  | { accepted: false; reason: "missing-key" | "busy" | "empty" | "offline" };

export interface PendingMemorySuggestion {
  id: string;
  content: string;
  category: string;
  scope: string;
  conflicts: readonly { id: string; content: string; reason: string }[];
}

export interface ChatControllerOptions {
  /** 测试注入：每标签页一个持久化适配器（独立的本地 base 与脏检测）。 */
  persistence?: ConversationPersistenceAdapter;
  /** 测试注入：跨标签页同步通道。 */
  syncChannel?: ConversationSyncChannel;
  /** 测试注入：模拟另一标签页的 sessionStorage（决定 tabId 与选中态）；缺省用浏览器 sessionStorage。 */
  session?: StorageLike | null;
  /**
   * 测试注入：以固定防抖覆盖自动保存调度（普通 300ms 合并 / 流式 1s 节流 /
   * 结算即时提交全部停用），便于测试用显式 flush 精确驱动提交。
   */
  autosaveDebounceMs?: number;
}

export function useChatController(options: ChatControllerOptions = {}) {
  const settings = useSettings();
  const projects = useProjects();
  const memory = useMemory();
  // 每标签页一个持久化适配器实例：本地 base 与脏检测是标签页级状态，
  // 这是跨标签页仲裁（重读共享 head、兄弟检测）的前提。
  const persistenceRef = useRef<ConversationPersistenceAdapter | null>(null);
  if (!persistenceRef.current) persistenceRef.current = options.persistence ?? createConversationPersistenceAdapter();
  const persistence = persistenceRef.current;
  const syncChannelRef = useRef<ConversationSyncChannel | null>(null);
  if (!syncChannelRef.current) syncChannelRef.current = options.syncChannel ?? createConversationSyncChannel();
  const syncChannel = syncChannelRef.current;
  const [state, dispatch] = useReducer(
    chatReducer,
    undefined,
    () => createInitialChatState(persistence.load(undefined, options.session)),
  );
  const stateRef = useRef(state);
  stateRef.current = state;
  // 同步接受守卫：dispatch 尚未反映到 state 前，同一 tick 的重复提交直接拒绝。
  const submissionGuardRef = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const outputPauseGateRef = useRef<ReturnType<typeof createOutputPauseGate> | null>(null);
  if (!outputPauseGateRef.current) outputPauseGateRef.current = createOutputPauseGate();
  const [outputPaused, setOutputPaused] = useState(false);
  const [pendingMemorySuggestion, setPendingMemorySuggestion] = useState<PendingMemorySuggestion | null>(null);
  const [quoteDraft, setQuoteDraft] = useState<QuoteDraft | null>(null);
  // 所有未解决分支都来自耐久 Ledger；Notice 逐个处理，任何新冲突都不会覆盖旧项。
  const [conflicts, setConflicts] = useState<ConversationConflictSignal[]>(() =>
    persistence.listConflictBranches().flatMap((branch) => {
      const loaded = persistence.readConflictBranch(branch.conversationId, undefined, branch.conflictId);
      return loaded ? [{
        conversationId: branch.conversationId,
        conflictId: branch.conflictId,
        title: loaded.conversation.title,
        revision: branch.branchRevision,
        baseRevision: branch.baseRevision,
        sharedRevision: branch.sharedRevision,
        writerId: branch.writerSessionId,
        savedAt: branch.updatedAt,
      }] : [];
    }));
  const conflict = conflicts[0] ?? null;
  const [resolvingConflict, setResolvingConflict] = useState(false);
  const waitUntilResumed = useCallback(() => outputPauseGateRef.current?.waitUntilResumed() ?? Promise.resolve(), []);

  // 提交 / 删除 / 冲突 / 恢复副本 / 压缩回调：提交与删除广播到跨标签页通道，冲突信号换上 notice 条，
  // 恢复副本（本地修改在其他标签页删除后幸存）换入 state 并提示，存储压力下的压缩成功一次性提示。
  const saveCallbacksRef = useRef<SaveConversationOptions | null>(null);
  if (!saveCallbacksRef.current) {
    saveCallbacksRef.current = {
      onCommit: (notice) => syncChannel.post({ type: "conversation_committed", ...notice }),
      onDelete: (notice) => syncChannel.post({ type: "conversation_deleted", ...notice }),
      onConflict: (signal) => setConflicts((current) => {
        const withoutSame = current.filter((item) => item.conflictId !== signal.conflictId);
        return [...withoutSame, signal].sort((left, right) => left.savedAt - right.savedAt);
      }),
      onWarning: (failure) => {
        recordFlushReport({ ok: false, results: { conversation: failure }, failedIds: ["conversation"] });
        dispatch({ type: "noticeSet", notice: "检测到损坏的会话检查点，已安全回退并修复" });
      },
      onRecovery: ({ conversationId, copy }) => {
        dispatch({ type: "conversationSynced", conversation: copy });
        dispatch({ type: "deleteConversation", conversationId });
        dispatch({ type: "noticeSet", notice: "远端已删除，已保留为恢复副本" });
      },
      onCompaction: () => {
        dispatch({ type: "noticeSet", notice: "存储空间不足，已压缩图片预览，全部文字内容保留" });
      },
    };
  }

  const readPersistedState = useCallback((): PersistedConversationState => {
    const current = stateRef.current;
    return {
      schemaVersion: 1,
      currentConversationId: current.currentConversationId,
      conversations: current.conversations,
    };
  }, []);

  const recordFailedResult = useCallback((result: PersistenceFlushResult) => {
    if (!result.ok) {
      recordFlushReport({ ok: false, results: { conversation: result }, failedIds: ["conversation"] });
      if (result.code === "storage-pressure") {
        // 渐进压缩重试后仍超限：除失败横幅外，引导使用既有导出 / 清理入口释放空间（无新 UI）。
        dispatch({ type: "noticeSet", notice: "存储空间不足，请导出或清理旧会话后重试" });
      }
    }
  }, []);

  // 自动保存调度状态：待决定时器句柄（生命周期 flush 须可取消）、上次提交
  // 时刻（流式节流锚点）、上一渲染的流式标记（识别 streaming→idle 结算跃迁）。
  const autosaveTimerRef = useRef<number | null>(null);
  const lastCommitAtRef = useRef(Date.now());
  const wasStreamingRef = useRef(false);
  const cancelAutosaveTimer = useCallback(() => {
    if (autosaveTimerRef.current !== null) {
      window.clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
  }, []);

  const flushConversationPersistence = useCallback((): PersistenceFlushResult => {
    // 生命周期 flush（pagehide / beforeunload / 构建更新激活）：取消待决的
    // 防抖/节流定时器并由本次同步 flush 接管提交——立即提交全部脏状态，绝不双提交。
    cancelAutosaveTimer();
    lastCommitAtRef.current = Date.now();
    let result: PersistenceFlushResult;
    try {
      // 无锁同步路径（pagehide / beforeunload 必须同步完成）：与走锁路径完全
      // 相同的"重读共享 head + 比较 base"检查，并发冲突退化为冲突分支。
      result = persistence.save(readPersistedState(), undefined, options.session, saveCallbacksRef.current ?? undefined);
    } catch (reason) {
      // 防抖保存和生命周期 flush 都不允许把存储异常抛成未捕获错误。
      result = flushFailureResult(reason);
    }
    recordFailedResult(result);
    // 页面退出应急胶囊：同步 flush 之后仍然脏的会话落入本标签页胶囊键（flush
    // 成功 ⇒ 空集合 ⇒ 移除胶囊键）；下次启动在写锁内对账。胶囊写失败同样只
    // 记录健康状态，绝不抛出——此刻页面可能正在销毁。
    const capsuleFailure = persistence.writeRecoveryCapsule(readPersistedState(), undefined, options.session);
    if (capsuleFailure) {
      recordFlushReport({ ok: false, results: { conversation: capsuleFailure }, failedIds: ["conversation"] });
    }
    return result;
  }, [persistence, readPersistedState, recordFailedResult, options.session, cancelAutosaveTimer]);

  // 启动胶囊对账（排他写锁内）：本标签页胶囊（崩溃/杀死但会话恢复）与租约已死的
  // 孤儿胶囊在此补交/物化——干净内容作为正常分片提交补交并换入 state（本地副本
  // 自加载起未被触碰才换入）；冲突或已删除内容以确定性 id 物化恢复副本并经
  // notice 条提示。对账按胶囊键幂等收敛，StrictMode 双跑绝不重复提交。
  useEffect(() => {
    void persistence
      .reconcileRecoveryCapsules(undefined, options.session, saveCallbacksRef.current ?? undefined)
      .then((outcome) => {
        for (const committed of outcome.committed) {
          const local = stateRef.current.conversations.find((conversation) => conversation.id === committed.conversationId);
          if (local && local !== committed.previous) continue;
          dispatch({ type: "conversationSynced", conversation: committed.conversation });
        }
        if (outcome.recovered.length) {
          for (const copy of outcome.recovered) {
            dispatch({ type: "conversationSynced", conversation: copy });
          }
          dispatch({ type: "noticeSet", notice: "检测到上次未保存的会话内容，已保留为恢复副本" });
        }
        for (const failure of outcome.failed) {
          recordFlushReport({ ok: false, results: { conversation: failure }, failedIds: ["conversation"] });
        }
      })
      .catch(() => undefined); // 对账绝不把异常抛成未捕获错误。
  }, [persistence, options.session]);

  // 防抖（普通）提交走排他 Web Lock 临界区；锁缺失时适配器内部退化为无锁
  // 路径（同样的兄弟检测）。单-flight：流式期间高频触发只保留最新一次，
  // 避免锁请求排队造成 head 反复重写。
  const arbitratedFlightRef = useRef({ running: false, queued: false });
  const arbitratedFlushRef = useRef<() => void>(() => undefined);
  arbitratedFlushRef.current = () => {
    const flight = arbitratedFlightRef.current;
    if (flight.running) {
      flight.queued = true;
      return;
    }
    flight.running = true;
    void persistence
      .saveArbitrated(readPersistedState, undefined, options.session, saveCallbacksRef.current ?? undefined)
      .catch((reason: unknown): PersistenceFlushResult => flushFailureResult(reason))
      .then((result) => {
        recordFailedResult(result);
        flight.running = false;
        if (flight.queued) {
          flight.queued = false;
          arbitratedFlushRef.current();
        }
      });
  };

  // 当前会话选中是纯标签页 UI 状态：只写本标签页 sessionStorage（best-effort，
  // 失败静默降级），绝不因此调度共享 checkpoint 提交——分片保存只跟随会话内容变化。
  useEffect(() => {
    persistence.persistSelection(state.currentConversationId, options.session);
  }, [persistence, state.currentConversationId, options.session]);

  // 自动保存调度：普通编辑 300ms 尾随合并；流式进行中锚定上次提交时刻的 1s
  // 节流（尾随边界提交，尾部增量不丢）；流式结算（done / error / 中断 ⇒
  // streaming→idle 跃迁）立即提交并绕过待决定时器。effect 清理保证卸载后
  // 定时器绝不触发。测试注入 autosaveDebounceMs 时退回固定防抖。
  useEffect(() => {
    const streamingActive = state.requestStatus === "streaming";
    const justSettled = wasStreamingRef.current && !streamingActive;
    wasStreamingRef.current = streamingActive;
    const decision = options.autosaveDebounceMs !== undefined
      ? options.autosaveDebounceMs
      : decideCheckpointDelay({
        streamingActive,
        justSettled,
        lastCommitAt: lastCommitAtRef.current,
        now: Date.now(),
      });
    if (decision === "immediate") {
      lastCommitAtRef.current = Date.now();
      arbitratedFlushRef.current();
      return undefined;
    }
    const timer = window.setTimeout(() => {
      if (autosaveTimerRef.current === timer) autosaveTimerRef.current = null;
      lastCommitAtRef.current = Date.now();
      arbitratedFlushRef.current();
    }, decision);
    autosaveTimerRef.current = timer;
    return () => {
      window.clearTimeout(timer);
      if (autosaveTimerRef.current === timer) autosaveTimerRef.current = null;
    };
  }, [state.conversations, state.requestStatus, options.autosaveDebounceMs]);

  // 跨标签页同步：BroadcastChannel 负责低延迟通知，V3 Head 的 storage 事件作为
  // 耐久事实源回退。任一通知到达时——本标签页对该会话干净则换入共享 head；
  // 本地脏则保持，等下次提交走冲突路径；绝不切换当前选中会话。流式中的会话
  // 延迟到流结束后再同步。远端删除到达时，本地干净则移除（选中回退保持既有
  // UX）；本地脏则保留内容，下次提交被 tombstone 拒绝时自动物化为恢复副本。
  const deferredSyncRef = useRef(new Set<string>());
  const syncRetryTimersRef = useRef(new Map<string, number>());
  const handleSyncConversation = useCallback(
    (conversationId: string) => {
      const local = stateRef.current.conversations.find((conversation) => conversation.id === conversationId);
      const outcome = persistence.reconcileRemoteCommit(conversationId, local);
      // 远端删除且本地干净 ⇒ deleted：移除（选中回退保持既有 UX）；本地脏 ⇒ stale：
      // 保留内容，下次提交被 tombstone 拒绝时自动物化为恢复副本。
      if (outcome.kind === "reload") dispatch({ type: "conversationSynced", conversation: outcome.conversation });
      else if (outcome.kind === "deleted") dispatch({ type: "deleteConversation", conversationId });
    },
    [persistence],
  );
  const requestSyncConversation = useCallback((conversationId: string) => {
    if (stateRef.current.requestStatus === "streaming") {
      deferredSyncRef.current.add(conversationId);
      return;
    }
    handleSyncConversation(conversationId);
  }, [handleSyncConversation]);
  const scheduleSyncRetry = useCallback((conversationId: string) => {
    const previous = syncRetryTimersRef.current.get(conversationId);
    if (previous !== undefined) window.clearTimeout(previous);
    const timer = window.setTimeout(() => {
      syncRetryTimersRef.current.delete(conversationId);
      requestSyncConversation(conversationId);
    }, STORAGE_SYNC_FALLBACK_MS);
    syncRetryTimersRef.current.set(conversationId, timer);
  }, [requestSyncConversation]);

  useEffect(() => {
    let ownIdentity = persistence.getReplicaIdentity(options.session);
    // 标签页租约：回到前台刷新，pagehide 尽力移除（tombstone GC 的活跃证据）。
    const onLeaseEvent = (event: Event): void => {
      if (event.type === "pagehide" || document.visibilityState === "visible") {
        persistence.setTabLease(event.type !== "pagehide", undefined, options.session);
      }
    };
    document.addEventListener("visibilitychange", onLeaseEvent);
    window.addEventListener("pagehide", onLeaseEvent);
    const unsubscribe = syncChannel.subscribe((message) => {
      if (message.type === "writer_claim") {
        if (message.writerSessionId !== ownIdentity.writerSessionId
          || message.documentInstanceId === ownIdentity.documentInstanceId) return;
        if (ownIdentity.documentInstanceId.localeCompare(message.documentInstanceId) < 0) {
          syncChannel.post({
            type: "writer_claim_ack",
            writerSessionId: ownIdentity.writerSessionId,
            documentInstanceId: ownIdentity.documentInstanceId,
            targetDocumentInstanceId: message.documentInstanceId,
          });
        } else {
          ownIdentity = persistence.rotateWriterIdentity(options.session);
          syncChannel.post({ type: "writer_claim", ...ownIdentity });
        }
        return;
      }
      if (message.type === "writer_claim_ack") {
        if (message.targetDocumentInstanceId !== ownIdentity.documentInstanceId
          || message.writerSessionId !== ownIdentity.writerSessionId) return;
        ownIdentity = persistence.rotateWriterIdentity(options.session);
        syncChannel.post({ type: "writer_claim", ...ownIdentity });
        return;
      }
      if (message.writerId === ownIdentity.writerSessionId) return;
      requestSyncConversation(message.conversationId);
      // 收到通知不等于 React 已完成换入；dispatch 与适配器登记之间可能被打断。
      // 保留一次幂等延迟复核，storage 事件只负责合并同一会话的计时器。
      scheduleSyncRetry(message.conversationId);
    });
    syncChannel.post({ type: "writer_claim", ...ownIdentity });
    return () => {
      unsubscribe();
      document.removeEventListener("visibilitychange", onLeaseEvent);
      window.removeEventListener("pagehide", onLeaseEvent);
    };
  }, [syncChannel, requestSyncConversation, scheduleSyncRetry, persistence, options.session]);

  useEffect(() => {
    const onStorage = (event: StorageEvent): void => {
      const key = event.key;
      if (!key?.startsWith(conversationStorageKeys.v3HeadPrefix)) return;
      const conversationId = key.slice(conversationStorageKeys.v3HeadPrefix.length);
      if (!conversationId) return;
      scheduleSyncRetry(conversationId);
    };
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("storage", onStorage);
      syncRetryTimersRef.current.forEach((timer) => window.clearTimeout(timer));
      syncRetryTimersRef.current.clear();
    };
  }, [scheduleSyncRetry]);

  useEffect(() => {
    if (state.requestStatus !== "idle" || !deferredSyncRef.current.size) return;
    const pending = [...deferredSyncRef.current];
    deferredSyncRef.current.clear();
    pending.forEach(handleSyncConversation);
  }, [state.requestStatus, handleSyncConversation]);

  const finishConflict = useCallback((conflictId: string) => {
    setConflicts((current) => current.filter((item) => item.conflictId !== conflictId));
  }, []);

  // 查看最新与保留副本都由持久化层完成耐久事务，成功后才换 State / 释放 Ledger。
  const resolveConflictByReload = useCallback(async () => {
    if (!conflict || resolvingConflict) return;
    setResolvingConflict(true);
    try {
      const result = await persistence.resolveConflictByReloadArbitrated(conflict.conversationId, conflict.conflictId);
      if (!result.ok) {
        recordFailedResult(result);
        return;
      }
      dispatch({ type: "conversationSynced", conversation: result.conversation });
      finishConflict(conflict.conflictId);
    } finally {
      setResolvingConflict(false);
    }
  }, [conflict, resolvingConflict, persistence, finishConflict, recordFailedResult]);

  const resolveConflictByCopy = useCallback(async () => {
    if (!conflict || resolvingConflict) return;
    setResolvingConflict(true);
    try {
      const result = await persistence.resolveConflictByCopyArbitrated(
        conflict.conversationId,
        conflict.conflictId,
        undefined,
        options.session,
        saveCallbacksRef.current ?? undefined,
      );
      if (!result.ok) {
        recordFailedResult(result);
        return;
      }
      dispatch({ type: "conversationSynced", conversation: result.copy });
      finishConflict(conflict.conflictId);
    } finally {
      setResolvingConflict(false);
    }
  }, [conflict, resolvingConflict, persistence, options.session, finishConflict, recordFailedResult]);

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

  const tryStartMessage = useCallback(
    (input: string, options: { attachments?: readonly Attachment[]; online?: boolean } = {}): MessageSubmissionResult => {
      if (options.online === false) return { accepted: false, reason: "offline" };
      const attachments = options.attachments ?? [];
      if (settings.agentMode) {
        if (!input.trim() && !attachments.length) return { accepted: false, reason: "empty" };
        if (state.requestStatus === "streaming" || submissionGuardRef.current) return { accepted: false, reason: "busy" };
        if (!hasBackendKey()) return { accepted: false, reason: "missing-key" };
        submissionGuardRef.current = true;
        void agentRun.sendAgentMessage(input, { attachments })
          .finally(() => {
            submissionGuardRef.current = false;
          });
        return { accepted: true, conversationId: state.currentConversationId ?? "" };
      }
      const quotedContent = quoteAwareContent(input.trim(), quoteDraft);
      if (!quotedContent && !attachments.length) return { accepted: false, reason: "empty" };
      if (state.requestStatus === "streaming" || submissionGuardRef.current) return { accepted: false, reason: "busy" };
      if (!hasBackendKey()) return { accepted: false, reason: "missing-key" };

      // 接受即冻结项目上下文，异步流式阶段不再读取项目状态。
      submissionGuardRef.current = true;
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

      void (async () => {
        try {
          const finished = await streamIntoMessage(assistantMessage, payload);
          await maybeGenerateTitle(conversationId, firstTurn, newUserMessage.content, finished?.content ?? "");
        } finally {
          submissionGuardRef.current = false;
        }
      })();
      return { accepted: true, conversationId };
    },
    [agentRun, hasBackendKey, maybeCreateReminder, maybeGenerateTitle, projects, quoteDraft, requestSettings, settings, state, streamIntoMessage],
  );

  const sendMessage = useCallback(
    async (input: string, options: { attachments?: readonly Attachment[] } = {}) => {
      const result = tryStartMessage(input, options);
      if (!result.accepted && result.reason === "missing-key") {
        dispatch({ type: "noticeSet", notice: "请先在连接设置中输入 DeepSeek API Key" });
      }
    },
    [tryStartMessage],
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

  // 删除会话：先在仲裁临界区耐久提交 tombstone，成功才移除 UI 状态；失败则
  // 会话保留，经 flush 失败链路记录健康状态并在 notice 条提示。流式期间保持
  // 既有行为（reducer 同样拒绝删除）。
  const deleteConversation = useCallback(
    (conversationId: string) => {
      if (stateRef.current.requestStatus === "streaming") return;
      void persistence
        // saveCallbacksRef 在首次渲染即初始化，此处必然非空。
        .deleteConversationArbitrated(conversationId, undefined, options.session, saveCallbacksRef.current!)
        .then((result) => {
          if (result.ok) {
            dispatch({ type: "deleteConversation", conversationId });
            return;
          }
          recordFailedResult(result);
          dispatch({ type: "noticeSet", notice: result.message });
        });
    },
    [persistence, options.session, recordFailedResult],
  );

  return {
    state,
    messages: selectCurrentMessages(state),
    outputPaused,
    pendingMemorySuggestion,
    quoteDraft,
    conflict,
    conflictCount: conflicts.length,
    resolvingConflict,
    resolveConflictByReload,
    resolveConflictByCopy,
    sendMessage,
    tryStartMessage,
    editAndResend,
    regenerate,
    continueGeneration,
    confirmAgentPlan: agentRun.confirmPlan,
    rerunAgentPhase: agentRun.rerunPhase,
    resumeAgentRun: agentRun.resumeRun,
    stopGeneration,
    pauseOutput,
    resumeOutput,
    newConversation: () => dispatch({ type: "newConversation" }),
    openConversation: (conversationId: string) => dispatch({ type: "openConversation", conversationId }),
    deleteConversation,
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
