import { useState } from "react";

import { agentRunSummary } from "../../domain/chat/agentTimeline";
import type { ChatMessage, TimelineStep } from "../../domain/chat/types";
import { useChat } from "../../contexts/ChatContext";
import { MarkdownContent } from "../../shared/markdown/MarkdownContent";
import { SearchBlock } from "../citations/SearchBlock";
import { Icon } from "../../shared/ui/Icon";

function statusLabel(step: TimelineStep): string {
  if (step.status === "running") return "工作中";
  if (step.status === "error") return "失败";
  if (step.status === "done") {
    return typeof step.durationMs === "number" ? `已完成 ${(step.durationMs / 1000).toFixed(1)}s` : "已完成";
  }
  return step.status ?? "";
}

function AgentCard({ step, message }: { step: TimelineStep; message: ChatMessage }) {
  const chat = useChat();
  const [collapsed, setCollapsed] = useState(Boolean(step.collapsed));
  const canRerun = Boolean(
    message.agentRunId && step.phase && step.phase !== "leader" && step.status !== "running" && !message.streaming,
  );
  return (
    <section className={`agent-step status-${step.status ?? "running"}`} data-agent-phase={step.phase}>
      <header className="agent-step-header">
        <button
          className="agent-step-toggle"
          type="button"
          aria-expanded={!collapsed}
          onClick={() => setCollapsed((value) => !value)}
        >
          <strong>{step.name || step.phase || "Agent"}</strong>
          <span className="agent-step-status">{statusLabel(step)}</span>
        </button>
        {canRerun && (
          <button
            className="agent-step-rerun"
            type="button"
            disabled={chat.state.requestStatus === "streaming"}
            onClick={() => void chat.rerunAgentPhase(message, step.phase as string)}
          >
            {step.status === "error" ? "重试" : "重跑"}
          </button>
        )}
      </header>
      {!collapsed && (
        <div className="agent-step-body">
          {step.reasoning && (
            <details className="agent-step-reasoning">
              <summary>推理过程</summary>
              <MarkdownContent content={step.reasoning} />
            </details>
          )}
          {(step.output || step.text) && <MarkdownContent content={step.output || step.text || ""} />}
          {step.notes?.map((note, index) => <p className="system-note" key={index}>{note}</p>)}
          {step.status === "running" && !step.output && !step.reasoning && <p className="response-placeholder">等待输出…</p>}
        </div>
      )}
    </section>
  );
}

export function AgentTimeline({ message }: { message: ChatMessage }) {
  const agentSteps = message.timeline.filter((step) => step.type === "agent");
  const searchSteps = message.timeline.filter((step) => step.type === "search" && step.search);
  if (!agentSteps.length && !searchSteps.length) return null;
  const summary = agentRunSummary(message.timeline).filter((entry) => entry.status !== "pending");

  return (
    <div className="agent-timeline" aria-label="Agent 执行过程">
      {summary.length > 0 && (
        <div className="agent-run-summary">
          <strong>{agentSteps.length} 个 Agent</strong>
          {summary.map((entry) => (
            <span className={`agent-summary-chip status-${entry.status}`} key={entry.phase}>
              {entry.label} {entry.status === "done" ? <Icon name="check" /> : entry.status === "error" ? <Icon name="error" /> : "…"}
            </span>
          ))}
        </div>
      )}
      {searchSteps.map((step) => (
        <SearchBlock key={step.id} search={step.search as NonNullable<typeof step.search>} streaming={step.status === "searching"} />
      ))}
      {agentSteps.map((step) => <AgentCard key={step.id ?? `${step.phase}-${step.status}`} step={step} message={message} />)}
    </div>
  );
}
