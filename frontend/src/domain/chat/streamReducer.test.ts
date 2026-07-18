import { describe, expect, it } from "vitest";

import { applyStreamEvent, createAssistantMessage, resetAssistantMessage } from "./streamReducer";

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
    expect(agent.timeline[0]).toMatchObject({ type: "agent_delta", phase: "coder", text: "patch" });
    expect(interrupted).toMatchObject({ phase: "interrupted", streaming: false, agentRunId: "run-1" });
  });

  it("resets an assistant message for regeneration", () => {
    const initial = applyStreamEvent(createAssistantMessage("message-reset"), { type: "content", text: "old" });
    const withError = applyStreamEvent(initial, { type: "search", search: { query: "q" } });
    const reset = resetAssistantMessage(withError);
    expect(reset).toMatchObject({ content: "", reasoning: "", phase: "idle", streaming: true, search: null, error: undefined });
    expect(reset.id).toBe("message-reset");
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
