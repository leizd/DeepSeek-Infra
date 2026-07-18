import type { ChatMessage } from "../../domain/chat/types";
import { searchRounds } from "../citations/citations";

export function elapsedSeconds(message: ChatMessage, nowMs: number): number {
  return Math.max(0, Math.round((nowMs - message.createdAt) / 1000));
}

export function activitySummaryText(message: ChatMessage, nowMs: number): string {
  const seconds = elapsedSeconds(message, nowMs);
  const rounds = searchRounds(message.search).length;
  if (message.streaming) {
    const prefix = message.phase === "agent" || message.agentRunId ? "Agent 工作中" : "思考中";
    return `${prefix} ${seconds}s`;
  }
  const parts = [`已思考 ${seconds}s`];
  if (rounds > 0) parts.push(`搜索 ${rounds} 次`);
  return parts.join(" · ");
}

export function messageHasActivity(message: ChatMessage): boolean {
  return Boolean(
    message.reasoning.trim()
    || message.systemNotes.length
    || message.timeline.length
    || message.search
    || message.interrupted
    || (message.agentRunId && message.streaming),
  );
}
