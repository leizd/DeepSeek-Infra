import { useState } from "react";

import {
  AGENT_PHASE_IDS,
  agentPlanForPreset,
  normalizeEditableAgentPlan,
  type AgentPlanItem,
} from "../../api/agentRunApi";
import { useChat } from "../../contexts/ChatContext";
import type { ChatMessage } from "../../domain/chat/types";

const PHASE_LABELS: Record<string, string> = {
  researcher: "资料检索",
  coder: "代码分析",
  reasoner: "推理",
  critic: "反驳审查",
};

const PRESETS = [
  { id: "full", label: "完整 4-Agent" },
  { id: "code", label: "仅代码分析" },
  { id: "research", label: "仅资料检索" },
  { id: "critic", label: "仅反驳审查" },
] as const;

function planFromMessage(message: ChatMessage): AgentPlanItem[] {
  return (message.agentPlan ?? []).flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    const id = typeof record.id === "string" ? record.id : "";
    if (!AGENT_PHASE_IDS.includes(id as (typeof AGENT_PHASE_IDS)[number])) return [];
    return [{
      id,
      task: typeof record.task === "string" ? record.task : "",
      depends_on: Array.isArray(record.depends_on) ? record.depends_on.filter((dep): dep is string => typeof dep === "string") : undefined,
    }];
  });
}

export function AgentPlanEditor({ message }: { message: ChatMessage }) {
  const chat = useChat();
  const [rows, setRows] = useState<AgentPlanItem[]>(() => planFromMessage(message));
  const busy = chat.state.requestStatus === "streaming";

  function updateRow(index: number, patch: Partial<AgentPlanItem>) {
    setRows((current) => current.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)));
  }

  function addRow() {
    const missing = AGENT_PHASE_IDS.find((phase) => !rows.some((row) => row.id === phase)) ?? "critic";
    setRows((current) => (current.length >= 4 ? current : [...current, { id: missing, task: "" }]));
  }

  return (
    <div className="agent-plan-workbench" aria-label="Agent 执行计划">
      <div className="agent-plan-header">
        <strong>{message.agentPlanLabel || "Agent 执行计划"}</strong>
        <span>等待确认</span>
      </div>
      <div className="agent-plan-presets">
        {PRESETS.map((preset) => (
          <button key={preset.id} type="button" disabled={busy} onClick={() => setRows(agentPlanForPreset(preset.id))}>
            {preset.label}
          </button>
        ))}
      </div>
      <ul className="agent-plan-rows">
        {rows.map((row, index) => (
          <li className="agent-plan-row" key={`${row.id}-${index}`}>
            <select
              aria-label="选择 Agent"
              value={row.id}
              disabled={busy}
              onChange={(event) => updateRow(index, { id: event.target.value })}
            >
              {AGENT_PHASE_IDS.map((phase) => (
                <option key={phase} value={phase}>{PHASE_LABELS[phase] ?? phase}</option>
              ))}
            </select>
            <textarea
              aria-label="Agent 任务"
              rows={2}
              maxLength={500}
              value={row.task}
              disabled={busy}
              onChange={(event) => updateRow(index, { task: event.target.value })}
            />
            <button
              type="button"
              aria-label="移除 Agent"
              disabled={busy || rows.length <= 1}
              onClick={() => setRows((current) => current.filter((_, rowIndex) => rowIndex !== index))}
            >
              移除
            </button>
          </li>
        ))}
      </ul>
      <div className="agent-plan-actions">
        <button type="button" disabled={busy || rows.length >= 4} onClick={addRow}>添加 Agent</button>
        <button
          className="primary"
          type="button"
          disabled={busy}
          onClick={() => void chat.confirmAgentPlan(message, normalizeEditableAgentPlan(rows))}
        >
          确认执行
        </button>
      </div>
    </div>
  );
}
