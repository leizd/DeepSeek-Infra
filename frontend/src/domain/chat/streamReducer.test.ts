import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { applyStreamEvent, createAssistantMessage } from "./streamReducer";

describe("applyStreamEvent", () => {
  it("moves through reasoning, search, content and done without mutating prior messages", () => {
    const initial = createAssistantMessage("message-1");
    const thinking = applyStreamEvent(initial, { type: "reasoning", text: "plan" });
    const searching = applyStreamEvent(thinking, { type: "search", search: { query: "docs" } });
    const answering = applyStreamEvent(searching, { type: "content", text: "hello" });
    const done = applyStreamEvent(answering, { type: "done", diagnostics: { traceId: "trace-1" } });

    expect(initial).toMatchObject({ content: "", reasoning: "", phase: "idle", streaming: true });
    expect(thinking).toMatchObject({ reasoning: "plan", phase: "thinking" });
    expect(searching).toMatchObject({ phase: "searching", search: { query: "docs" } });
    expect(answering).toMatchObject({ content: "hello", phase: "answering" });
    expect(done).toMatchObject({ content: "hello", phase: "done", streaming: false });
    expect(done.diagnostics).toEqual({ traceId: "trace-1" });
    expect(done.agentRunStatus).toBeUndefined();
  });

  it("keeps agent events scoped to the timeline and handles interruption", () => {
    const initial = createAssistantMessage("message-2");
    const agent = applyStreamEvent(initial, {
      type: "agent_delta",
      phase: "coder",
      text: "patch",
      payload: { type: "agent_delta", phase: "coder", text: "patch" },
    });
    const interrupted = applyStreamEvent(agent, { type: "run_status", status: "cancelled", runId: "run-1" });

    expect(agent.content).toBe("");
    expect(agent.timeline).toHaveLength(1);
    expect(agent.timeline[0]).toMatchObject({ type: "agent", phase: "coder", output: "patch", status: "running" });
    expect(interrupted).toMatchObject({ phase: "interrupted", streaming: false, agentRunId: "run-1" });
  });

  it("tracks the event cursor and fills the awaiting_plan placeholder", () => {
    const initial = createAssistantMessage("message-cursor");
    const withCursor = applyStreamEvent(initial, { type: "run_status", status: "awaiting_plan", runId: "run-9", index: 7 });
    expect(withCursor).toMatchObject({
      agentRunLastEventIndex: 7,
      agentRunStatus: "awaiting_plan",
      content: "Agent 计划已生成，等待确认执行。",
    });
    const stale = applyStreamEvent(withCursor, { type: "content", text: "x", index: 3 });
    expect(stale.agentRunLastEventIndex).toBe(7);
  });

  it.each(["running", "done", "failed", "cancelled"] as const)("removes the generated confirmation notice when the run becomes %s", (status) => {
    const waiting = applyStreamEvent(createAssistantMessage("confirmed"), { type: "run_status", status: "awaiting_plan", runId: "run-9" });
    const resumed = applyStreamEvent(waiting, { type: "run_status", status, runId: "run-9" });
    const answering = applyStreamEvent(resumed, { type: "content", text: "Controlled final answer" });
    const done = applyStreamEvent(answering, { type: "done" });

    expect(waiting.content).toBe("Agent 计划已生成，等待确认执行。");
    expect(resumed.content).toBe("");
    expect(done.content).toBe("Controlled final answer");
  });

  it.each([
    { status: "done", content: "Retained final answer" },
    { status: "done", content: "Agent 计划已生成，等待确认执行。" },
    { status: "awaiting_plan", content: "Retained answer before an edit" },
  ] as const)("retains real answer text across resumption ($status / $content)", ({ status, content }) => {
    const message = { ...createAssistantMessage("retained"), agentRunId: "run-1", agentRunStatus: status, content };
    const resumed = applyStreamEvent(message, { type: "run_status", status: "running", runId: "run-1" });

    expect(resumed.content).toBe(content);
    expect(message.content).toBe(content);
  });

  it("records terminal errors and ignores forward-compatible unknown events", () => {
    const initial = createAssistantMessage("message-3");
    const unknown = applyStreamEvent(initial, {
      type: "unknown",
      originalType: "future_event",
      payload: { type: "future_event" },
    });
    const failed = applyStreamEvent(unknown, { type: "error", error: "blocked", code: "policy" });

    expect(unknown).toBe(initial);
    expect(failed).toMatchObject({ phase: "error", streaming: false, error: "blocked", errorCode: "policy" });
  });

  it("marks only an actual Agent Run as done", () => {
    const initial = { ...createAssistantMessage("message-agent"), agentRunId: "run-1", agentRunStatus: "running" };
    const done = applyStreamEvent(initial, { type: "done", runId: "run-1" });
    expect(done.agentRunStatus).toBe("done");
  });
});


describe("native complete Agent outputs", () => {
  it("retains actual native public output frames as completed rerunnable Agent cards", () => {
    const run = JSON.parse(readFileSync(new URL("../../../../tests/native/agent_public_browser_v1.json", import.meta.url), "utf8")) as {
      runId: string; events: Record<string, unknown>[];
    };
    const initial = createAssistantMessage("native-complete");
    let message = initial;
    for (const payload of run.events.filter((event) => event.type === "agent_output")) {
      message = applyStreamEvent(message, { type: "agent_output", runId: run.runId, index: payload.index as number, payload });
    }
    message = applyStreamEvent(message, { type: "done", runId: run.runId });
    expect(initial.timeline).toEqual([]);
    expect(message.timeline.map((step) => ({ phase: step.phase, status: step.status }))).toEqual([
      { phase: "coder", status: "done" }, { phase: "critic", status: "done" },
    ]);
    expect(message.timeline.every((step) => Boolean(step.output?.includes("Controlled public task summary")))).toBe(true);
    expect(message).toMatchObject({ streaming: false, agentRunId: run.runId, agentRunStatus: "done" });
  });

  it("updates an existing completed legacy card without duplicating it or exposing private fields", () => {
    const running = applyStreamEvent(createAssistantMessage("legacy"), {
      type: "agent_delta", phase: "coder", text: "draft", payload: {},
    });
    const completed = applyStreamEvent(running, { type: "agent", phase: "coder", status: "done", payload: {} });
    const result = applyStreamEvent(completed, { type: "agent_output", payload: {
      phase: "coder", output: { id: "coder", name: "代码实现 Agent", content: "complete public output", latencyMs: 125,
        reasoning_content: "private provider reasoning", apiKey: "private credential" },
    } });
    expect(result.timeline).toHaveLength(1);
    expect(result.timeline[0]).toMatchObject({ name: "代码实现 Agent", output: "complete public output", status: "done", durationMs: 125 });
    expect(completed.timeline[0].output).toBe("draft");
    expect(JSON.stringify(result.timeline)).not.toMatch(/private provider reasoning|private credential/);
  });

  it("keeps a failed output visibly failed and preserves the final answer until explicit reset", () => {
    const initial = { ...createAssistantMessage("failed-output"), content: "retained final" };
    const failed = applyStreamEvent(initial, { type: "agent_output", payload: {
      phase: "critic", output: { id: "critic", name: "反驳审查 Agent", failed: true, content: "该 Agent 执行失败：HTTP 401", duration_ms: 90 },
    } });
    expect(failed.timeline[0]).toMatchObject({ phase: "critic", status: "error", durationMs: 90, output: "该 Agent 执行失败：HTTP 401" });
    expect(failed.content).toBe("retained final");
    expect(initial.timeline).toHaveLength(0);
  });

  it.each([
    { phase: "critic", output: { id: "coder", content: "wrong role" } },
    { phase: "unknown", output: { id: "unknown", content: "unsupported role" } },
    { phase: "critic", output: { id: "critic", content: "bad failure type", failed: "false" } },
    { phase: "critic", output: null },
  ])("ignores invalid or rebound native output %j", (payload) => {
    const initial = createAssistantMessage("invalid-output");
    expect(applyStreamEvent(initial, { type: "agent_output", payload }).timeline).toEqual([]);
  });

  it("bounds public output text and does not turn a completed message back into a running one", () => {
    const done = applyStreamEvent(createAssistantMessage("bounded-output"), { type: "done", content: "final" });
    const result = applyStreamEvent(done, { type: "agent_output", payload: {
      phase: "coder", output: { id: "coder", content: "x".repeat(70_000) },
    } });
    expect(result.timeline[0].output).toHaveLength(60_000);
    expect(result).toMatchObject({ phase: "done", streaming: false, content: "final" });
  });
});
