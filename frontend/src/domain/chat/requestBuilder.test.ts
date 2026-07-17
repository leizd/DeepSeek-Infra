import { describe, expect, it } from "vitest";

import { buildChatPayload } from "./requestBuilder";
import type { ChatMessage } from "./types";

function message(role: "user" | "assistant", content: string): ChatMessage {
  return {
    id: `${role}-${content}`,
    role,
    content,
    reasoning: "",
    createdAt: 1,
    phase: "done",
    streaming: false,
    attachments: [],
    timeline: [],
    systemNotes: [],
  };
}

describe("buildChatPayload", () => {
  it("builds a normal chat payload without leaking unused search credentials", () => {
    const payload = buildChatPayload([message("user", "before"), message("assistant", "answer")], message("user", "next"), {
      apiKey: "  secret  ",
      tavilyApiKey: "search-secret",
      model: "deepseek-v4-pro",
      thinkingEnabled: true,
      searchEnabled: false,
    });

    expect(payload).toMatchObject({ apiKey: "secret", model: "deepseek-v4-pro", agentMode: false, stream: true });
    expect(payload.tavilyApiKey).toBeUndefined();
    expect(payload.messages).toEqual([
      { role: "user", content: "before" },
      { role: "assistant", content: "answer" },
      { role: "user", content: "next" },
    ]);
  });
});
