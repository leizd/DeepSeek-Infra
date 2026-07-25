import { isActiveRunStatus } from "../../api/agentRunApi";
import type { ChatMessage } from "../chat/types";

/** 生成被页面关闭/刷新打断时追加的系统说明；去重追加，重复保存只出现一次。 */
export const INTERRUPTED_CHECKPOINT_NOTE = "页面关闭或刷新时生成被中断。";

/** 仍处于生成途中、被杀死后不能谎称"已完成"的 phase。 */
const IN_FLIGHT_PHASES: readonly string[] = ["thinking", "searching", "tool", "answering"];

export function appendSystemNoteOnce(notes: readonly string[], note: string): readonly string[] {
  return notes.includes(note) ? notes : [...notes, note];
}

/**
 * 把一条消息规范化为可持久化/可恢复的诚实形态：
 * - 任何消息落盘时都不再处于 streaming；
 * - 生成途中被杀死的助手消息（仍在 streaming，或 phase 停留在进行态）标记为 interrupted，
 *   保留已生成的部分 content / reasoning，并追加一条系统说明（按精确串去重），
 *   使恢复后的会话提供"继续生成"而不是伪装成仍在回答；
 * - 例外：绑定活跃 Agent Run（agentRunStatus 仍活跃）的消息不在本地打断——
 *   它的真实状态以服务器为准，恢复时由 useAgentRun 的服务器对账逻辑重连或结算；
 *   agentRunStatus 已终态的消息按普通消息处理。
 * 保存与加载路径共用本函数，幂等：重复调用结果不变。
 */
export function checkpointMessage(message: ChatMessage): ChatMessage {
  if (message.agentRunId && isActiveRunStatus(message.agentRunStatus)) {
    return { ...message, streaming: false };
  }
  if (message.role === "assistant" && (message.streaming || IN_FLIGHT_PHASES.includes(message.phase))) {
    return {
      ...message,
      streaming: false,
      phase: "interrupted",
      interrupted: true,
      systemNotes: appendSystemNoteOnce(message.systemNotes, INTERRUPTED_CHECKPOINT_NOTE),
    };
  }
  return { ...message, streaming: false };
}
