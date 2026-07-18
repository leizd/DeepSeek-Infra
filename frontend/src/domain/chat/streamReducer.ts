import {
  appendTimelineAgent,
  appendTimelineAgentDelta,
  appendTimelineAgentNote,
  appendTimelineAgentReasoning,
  mergeAgentSearchStep,
  resetTimelineAgentPhase,
} from "./agentTimeline";
import type {
  AgentStreamEvent,
  ChatMessage,
  ChatStreamEvent,
  JsonRecord,
  StreamPhase,
  TimelineStep,
} from "./types";

export function createAssistantMessage(id: string): ChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    reasoning: "",
    createdAt: Date.now(),
    phase: "idle",
    streaming: true,
    attachments: [],
    timeline: [],
    systemNotes: [],
  };
}

export function resetAssistantMessage(message: ChatMessage): ChatMessage {
  return {
    ...message,
    content: "",
    reasoning: "",
    phase: "idle",
    streaming: true,
    interrupted: false,
    timeline: [],
    systemNotes: [],
    search: null,
    diagnostics: null,
    error: undefined,
    errorCode: undefined,
  };
}

function agentTimelineStep(event: AgentStreamEvent): TimelineStep {
  return {
    type: event.type,
    phase: event.phase,
    status: event.status,
    text: event.text,
    payload: event.payload,
  };
}

function applyAgentEvent(timeline: readonly TimelineStep[], event: AgentStreamEvent): readonly TimelineStep[] {
  switch (event.type) {
    case "agent":
      return appendTimelineAgent(timeline, event);
    case "agent_delta":
      return appendTimelineAgentDelta(timeline, event);
    case "agent_reasoning":
      return appendTimelineAgentReasoning(timeline, event);
    case "agent_note":
      return appendTimelineAgentNote(timeline, event);
    case "agent_search":
      return mergeAgentSearchStep(timeline, event);
    default:
      return [...timeline, agentTimelineStep(event)];
  }
}

function phaseForRunStatus(status: string, current: StreamPhase): StreamPhase {
  if (status === "done") return "done";
  if (status === "failed") return "error";
  if (status === "cancelled" || status === "interrupted") return "interrupted";
  return current === "answering" ? current : "agent";
}

function recordPayload(type: string, payload: JsonRecord): TimelineStep {
  return { type, payload };
}

export function applyStreamEvent(message: ChatMessage, event: ChatStreamEvent): ChatMessage {
  const next = applyStreamEventBody(message, event);
  if (typeof event.index === "number" && event.index > (message.agentRunLastEventIndex ?? -1)) {
    return { ...next, agentRunLastEventIndex: event.index };
  }
  return next;
}

function applyStreamEventBody(message: ChatMessage, event: ChatStreamEvent): ChatMessage {
  switch (event.type) {
    case "reasoning":
      return {
        ...message,
        phase: "thinking",
        reasoning: message.reasoning + event.text,
      };

    case "content":
      return {
        ...message,
        phase: "answering",
        content: message.content + event.text,
      };

    case "system_note":
      return {
        ...message,
        systemNotes: event.text.trim() ? [...message.systemNotes, event.text.trim()] : message.systemNotes,
      };

    case "search":
      return {
        ...message,
        phase: "searching",
        search: event.search,
        timeline: [...message.timeline, recordPayload("search", event.search ?? {})],
      };

    case "agent":
    case "agent_delta":
    case "agent_reasoning":
    case "agent_note":
    case "agent_search":
      return {
        ...message,
        phase: event.type === "agent_search" ? "searching" : "agent",
        timeline: applyAgentEvent(message.timeline, event),
      };

    case "memory_suggestion":
      return {
        ...message,
        timeline: [...message.timeline, recordPayload(event.type, event.payload)],
      };

    case "run_status": {
      const phase = phaseForRunStatus(event.status, message.phase);
      return {
        ...message,
        phase,
        streaming: !["done", "failed", "cancelled", "interrupted"].includes(event.status),
        agentRunId: event.runId ?? message.agentRunId,
        agentRunStatus: event.status,
        content: event.status === "awaiting_plan" && !message.content ? "Agent 计划已生成，等待确认执行。" : message.content,
      };
    }

    case "agent_plan":
      return {
        ...message,
        phase: "agent",
        agentRunId: event.runId ?? message.agentRunId,
        agentPlan: event.plan,
        agentPlanLabel: event.label ?? "",
      };

    case "final_reset":
      if (event.scope !== "final_answer") return message;
      return {
        ...message,
        content: "",
        diagnostics: null,
        phase: "agent",
        streaming: true,
      };

    case "agent_reset":
      return {
        ...message,
        phase: "agent",
        diagnostics: null,
        timeline: event.phase ? resetTimelineAgentPhase(message.timeline, event.phase) : message.timeline,
      };

    case "agent_output":
      return message;

    case "done":
      return {
        ...message,
        phase: "done",
        streaming: false,
        content: event.content ?? message.content,
        reasoning: event.reasoning ?? message.reasoning,
        model: event.model ?? message.model,
        diagnostics: event.diagnostics ?? message.diagnostics,
        usage: event.usage ?? message.usage,
        agentRunId: event.runId ?? message.agentRunId,
        agentRunStatus: message.agentRunId ? "done" : message.agentRunStatus,
        error: undefined,
        errorCode: undefined,
      };

    case "error":
      return {
        ...message,
        phase: "error",
        streaming: false,
        error: event.error,
        errorCode: event.code,
        agentRunId: event.runId ?? message.agentRunId,
        agentRunStatus: "failed",
      };

    case "unknown":
      return message;
  }
}
