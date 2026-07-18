import { describe, expect, it, vi } from "vitest";

import {
  agentPlanForPreset,
  agentRunStreamUrl,
  confirmAgentPlan,
  createAgentRun,
  isActiveRunStatus,
  normalizeAgentPlanItem,
  normalizeAgentPreset,
  normalizeAgentRun,
  normalizeEditableAgentPlan,
  rerunAgentPhase,
} from "./agentRunApi";
import { HttpClient } from "./httpClient";

function fakeClient(payload: unknown): { client: HttpClient; fetchImpl: ReturnType<typeof vi.fn> } {
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 }));
  return { client: new HttpClient({ fetchImpl }), fetchImpl };
}

describe("createAgentRun", () => {
  it("posts payload with agentMode forced and normalizes the run", async () => {
    const { client, fetchImpl } = fakeClient({
      runId: "run_abc",
      run: { runId: "run_abc", status: "planning", nextIndex: 0, plan: [{ id: "researcher", task: "查资料" }] },
    });
    const result = await createAgentRun({
      payload: { messages: [{ role: "user", content: "q" }], model: "deepseek-v4-pro" },
      confirmPlan: true,
      agentPreset: "full",
      conversationId: "c1",
      messageId: "m1",
    }, client);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe("/api/agent-runs");
    const body = JSON.parse(String(init.body));
    expect(body.payload.agentMode).toBe(true);
    expect(body).toMatchObject({ confirmPlan: true, agentPreset: "full", conversationId: "c1", messageId: "m1" });
    expect(result.runId).toBe("run_abc");
    expect(result.run.plan[0]).toMatchObject({ id: "researcher", task: "查资料" });
  });
});

describe("confirmAgentPlan / rerunAgentPhase", () => {
  it("posts the edited plan to the plan action", async () => {
    const { client, fetchImpl } = fakeClient({ started: true, run: { runId: "r", status: "running" } });
    const result = await confirmAgentPlan("run_1", { payload: { apiKey: "k" }, plan: [{ id: "coder", task: "写代码" }] }, client);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe("/api/agent-runs/run_1/plan");
    expect(JSON.parse(String(init.body))).toEqual({ payload: { apiKey: "k" }, plan: [{ id: "coder", task: "写代码" }] });
    expect(result).toMatchObject({ started: true, run: { status: "running" } });
  });

  it("posts rerun with agent id and resynthesize default", async () => {
    const { client, fetchImpl } = fakeClient({ started: true, run: { runId: "r", status: "running" } });
    await rerunAgentPhase("run_1", { payload: {}, agentId: "critic" }, client);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe("/api/agent-runs/run_1/rerun");
    expect(JSON.parse(String(init.body))).toEqual({ payload: {}, agentId: "critic", resynthesize: true });
  });
});

describe("normalizers", () => {
  it("normalizeAgentRun filters invalid plan items and unknown statuses", () => {
    const run = normalizeAgentRun({
      runId: "r",
      status: "weird",
      plan: [{ id: "unknown", task: "x" }, { id: "coder", task: "y" }, "garbage"],
    });
    expect(run.status).toBe("failed");
    expect(run.plan).toEqual([{ id: "coder", task: "y", depends_on: undefined }]);
  });

  it("normalizeAgentPlanItem requires a known phase id", () => {
    expect(normalizeAgentPlanItem({ id: "nope", task: "x" })).toBeNull();
    expect(normalizeAgentPlanItem({ id: "critic", task: "t", depends_on: ["coder", 1] })).toEqual({
      id: "critic",
      task: "t",
      depends_on: ["coder"],
    });
  });

  it("normalizeEditableAgentPlan dedupes, caps and cleans depends_on", () => {
    const plan = normalizeEditableAgentPlan([
      { id: "researcher", task: "  检索  " },
      { id: "researcher", task: "重复" },
      { id: "critic", task: "审查", depends_on: ["researcher"] },
      { id: "coder", task: "编码", depends_on: ["critic", "missing", "coder"] },
    ]);
    expect(plan).toHaveLength(3);
    expect(plan[0]).toEqual({ id: "researcher", task: "检索" });
    expect(plan[1]).toEqual({ id: "critic", task: "审查", depends_on: ["researcher"] });
    expect(plan[2]).toEqual({ id: "coder", task: "编码" });
  });

  it("normalizeEditableAgentPlan falls back to the full preset when empty", () => {
    expect(normalizeEditableAgentPlan([]).map((item) => item.id)).toEqual(["researcher", "coder", "reasoner", "critic"]);
  });

  it("agentPlanForPreset builds preset plans", () => {
    expect(agentPlanForPreset("code")).toHaveLength(1);
    expect(agentPlanForPreset("full").at(-1)?.depends_on).toContain("reasoner");
    expect(agentPlanForPreset("unknown")).toHaveLength(4);
  });

  it("normalizeAgentPreset and isActiveRunStatus", () => {
    expect(normalizeAgentPreset("auto")).toBe("auto");
    expect(normalizeAgentPreset("plan")).toBe("plan");
    expect(normalizeAgentPreset("bogus")).toBe("full");
    expect(isActiveRunStatus("running")).toBe(true);
    expect(isActiveRunStatus("awaiting_plan")).toBe(false);
    expect(isActiveRunStatus(undefined)).toBe(false);
  });

  it("agentRunStreamUrl encodes the cursor", () => {
    expect(agentRunStreamUrl("run_1", 41)).toBe("/api/agent-runs/run_1/stream?after=41");
    expect(agentRunStreamUrl("run_1", Number.NaN)).toBe("/api/agent-runs/run_1/stream?after=-1");
  });
});
