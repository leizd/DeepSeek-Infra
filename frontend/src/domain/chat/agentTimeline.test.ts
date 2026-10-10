import { describe, expect, it } from "vitest";

import {
  agentExecutionReport,
  agentRunSummary,
  agentStepId,
  appendTimelineAgent,
  appendTimelineAgentDelta,
  appendTimelineAgentNote,
  appendTimelineAgentReasoning,
  mergeAgentSearchStep,
  normalizeTimeline,
  resetTimelineAgentPhase,
  shouldCollapseAgentStep,
  TIMELINE_MAX_STEPS,
} from "./agentTimeline";
import type { AgentStreamEvent, ChatMessage, TimelineStep } from "./types";

function agentEvent(overrides: Partial<AgentStreamEvent> = {}): AgentStreamEvent {
  return { type: "agent", phase: "researcher", status: "running", payload: {}, ...overrides };
}

function agentCard(phase: string, status = "running"): TimelineStep {
  return { type: "agent", id: agentStepId(phase), phase, status, name: phase, notes: [] };
}

describe("appendTimelineAgent", () => {
  it("creates a card and replaces the running one on done", () => {
    let timeline = appendTimelineAgent([], agentEvent({ payload: { name: "Researcher" } }));
    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({ phase: "researcher", status: "running", name: "Researcher" });

    timeline = appendTimelineAgentDelta(timeline, { type: "agent_delta", phase: "researcher", text: "正文片段", payload: {} });
    timeline = appendTimelineAgent(timeline, agentEvent({ status: "done", text: "完成", payload: { durationMs: 1200 } }));
    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({ status: "done", durationMs: 1200, output: "正文片段" });
  });

  it("starts a new card when the previous one is already terminal", () => {
    let timeline = appendTimelineAgent([], agentEvent({ status: "done", text: "t", phase: "leader" }));
    timeline = appendTimelineAgent(timeline, agentEvent({ phase: "leader", payload: {} }));
    expect(timeline).toHaveLength(2);
    expect(timeline[0].id).not.toBe(timeline[1].id);
  });
});

describe("delta / reasoning / note accumulation", () => {
  it("accumulates into the running card and caps notes", () => {
    let timeline = appendTimelineAgent([], agentEvent());
    for (let index = 0; index < 25; index += 1) {
      timeline = appendTimelineAgentNote(timeline, { type: "agent_note", phase: "researcher", text: `note-${index}`, payload: {} });
    }
    expect(timeline[0].notes).toHaveLength(20);
    expect(timeline[0].notes?.[0]).toBe("note-5");

    timeline = appendTimelineAgentReasoning(timeline, { type: "agent_reasoning", phase: "researcher", text: "思考", payload: {} });
    expect(timeline[0].reasoning).toBe("思考");
  });

  it("creates placeholders when deltas arrive before the card", () => {
    const timeline = appendTimelineAgentDelta([], { type: "agent_delta", phase: "coder", text: "x", payload: {} });
    expect(timeline[0]).toMatchObject({ phase: "coder", status: "running", output: "x" });
  });
});

describe("resetTimelineAgentPhase", () => {
  it("drops agent and search steps for the phase only", () => {
    const timeline: TimelineStep[] = [
      agentCard("researcher"),
      agentCard("coder"),
      { type: "search", id: "s-researcher-main", phase: "researcher", status: "done" },
      { type: "reasoning", text: "r" },
    ];
    const next = resetTimelineAgentPhase(timeline, "researcher");
    expect(next.map((step) => step.type)).toEqual(["agent", "reasoning"]);
    expect(next[0].phase).toBe("coder");
  });
});

describe("shouldCollapseAgentStep / agentRunSummary", () => {
  it("collapses done workers with content but never the leader", () => {
    expect(shouldCollapseAgentStep({ ...agentCard("coder", "done"), output: "x" })).toBe(true);
    expect(shouldCollapseAgentStep({ ...agentCard("leader", "done"), output: "x" })).toBe(false);
    expect(shouldCollapseAgentStep(agentCard("coder", "done"))).toBe(false);
    expect(shouldCollapseAgentStep(agentCard("coder", "running"))).toBe(false);
  });

  it("summarizes worker statuses in fixed order, excluding the leader", () => {
    const timeline = [agentCard("leader", "done"), agentCard("coder", "done"), agentCard("critic", "error")];
    expect(agentRunSummary(timeline)).toEqual([
      { phase: "researcher", label: "资料", status: "pending" },
      { phase: "coder", label: "代码", status: "done" },
      { phase: "reasoner", label: "推理", status: "pending" },
      { phase: "critic", label: "复核", status: "error" },
    ]);
  });
});

describe("agentExecutionReport", () => {
  function messageWith(timeline: TimelineStep[], content = "最终回答正文"): ChatMessage {
    return {
      id: "a1",
      role: "assistant",
      content,
      reasoning: "",
      createdAt: 1,
      phase: "done",
      streaming: false,
      attachments: [],
      timeline,
      systemNotes: [],
    };
  }

  it("returns empty without agent steps", () => {
    expect(agentExecutionReport(messageWith([]))).toBe("");
  });

  it("builds a markdown report with leader, worker sections and the final answer", () => {
    const report = agentExecutionReport(messageWith([
      { ...agentCard("leader", "done"), output: "任务拆解：三步走" },
      { ...agentCard("researcher", "done"), output: "## 摘要\n找到三条来源" },
      { ...agentCard("critic", "error"), output: "" },
    ]));
    expect(report).toContain("# Agent 执行报告");
    expect(report).toContain("## Leader 拆解");
    expect(report).toContain("任务拆解：三步走");
    expect(report).toContain("## Researcher 摘要");
    expect(report).toContain("找到三条来源");
    expect(report).toContain("## Critic 风险");
    expect(report).toContain("该 Agent 执行失败。");
    expect(report).toContain("## 最终回答");
    expect(report).toContain("最终回答正文");
  });
});

describe("normalizeTimeline", () => {
  it("marks running cards as interrupted errors and dedupes ids", () => {
    const normalized = normalizeTimeline([
      agentCard("coder"),
      agentCard("coder"),
      { type: "search", id: "s-1", phase: "coder", status: "searching" },
    ]);
    expect(normalized[0]).toMatchObject({ status: "error", id: "agent-coder" });
    expect(normalized[1]).toMatchObject({ status: "error", id: "agent-coder-2" });
    expect(normalized[0].notes?.at(-1)).toContain("页面已刷新");
    expect(normalized[2]).toMatchObject({ status: "error" });
  });
});

describe("mergeAgentSearchStep", () => {
  it("keys search steps per phase and round and updates in place", () => {
    let timeline = mergeAgentSearchStep([], {
      type: "agent_search",
      phase: "researcher",
      search: { status: "searching", query: "q" },
      payload: {},
    });
    expect(timeline).toHaveLength(1);
    timeline = mergeAgentSearchStep(timeline, {
      type: "agent_search",
      phase: "researcher",
      search: { status: "done", query: "q", results: [{ title: "t", url: "u" }] },
      payload: {},
    });
    expect(timeline).toHaveLength(1);
    expect(timeline[0]).toMatchObject({ status: "done" });
    timeline = mergeAgentSearchStep(timeline, { type: "agent_search", phase: "coder", search: { status: "done" }, payload: {} });
    expect(timeline).toHaveLength(2);
  });

  it("caps the timeline length", () => {
    let timeline: readonly TimelineStep[] = [];
    for (let index = 0; index < TIMELINE_MAX_STEPS + 5; index += 1) {
      timeline = mergeAgentSearchStep(timeline, {
        type: "agent_search",
        phase: "researcher",
        search: { status: "done", round: index },
        payload: {},
      });
    }
    expect(timeline.length).toBeLessThanOrEqual(TIMELINE_MAX_STEPS);
  });
});
