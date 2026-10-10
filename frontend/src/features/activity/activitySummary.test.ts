import { describe, expect, it } from "vitest";

import { activitySummaryText, elapsedSeconds, messageHasActivity } from "./activitySummary";
import type { ChatMessage } from "../../domain/chat/types";

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "",
    reasoning: "",
    createdAt: 1_000,
    phase: "done",
    streaming: false,
    attachments: [],
    timeline: [],
    systemNotes: [],
    ...overrides,
  };
}

describe("elapsedSeconds / activitySummaryText", () => {
  it("computes non-negative elapsed seconds", () => {
    expect(elapsedSeconds(message(), 11_000)).toBe(10);
    expect(elapsedSeconds(message(), 500)).toBe(0);
  });

  it("labels streaming messages by mode", () => {
    expect(activitySummaryText(message({ streaming: true, phase: "agent" }), 6_000)).toBe("Agent 工作中 5s");
    expect(activitySummaryText(message({ streaming: true, agentRunId: "run_1" }), 6_000)).toBe("Agent 工作中 5s");
    expect(activitySummaryText(message({ streaming: true }), 6_000)).toBe("思考中 5s");
  });

  it("summarizes finished messages with search rounds", () => {
    const withSearch = message({ search: { rounds: [{ round: 1 }, { round: 2 }] } });
    expect(activitySummaryText(withSearch, 21_000)).toBe("已思考 20s · 搜索 2 次");
    expect(activitySummaryText(message(), 21_000)).toBe("已思考 20s");
  });
});

describe("messageHasActivity", () => {
  it("detects activity sources", () => {
    expect(messageHasActivity(message())).toBe(false);
    expect(messageHasActivity(message({ reasoning: "r" }))).toBe(true);
    expect(messageHasActivity(message({ systemNotes: ["n"] }))).toBe(true);
    expect(messageHasActivity(message({ timeline: [{ type: "agent" }] }))).toBe(true);
    expect(messageHasActivity(message({ search: { query: "q" } }))).toBe(true);
    expect(messageHasActivity(message({ interrupted: true }))).toBe(true);
    expect(messageHasActivity(message({ agentRunId: "run_1", streaming: true }))).toBe(true);
  });
});
