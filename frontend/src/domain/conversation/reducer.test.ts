import { describe, expect, it } from "vitest";

import {
  createConversation,
  replaceConversationMessages,
  sortConversations,
  titleFromMessages,
  withFavoriteToggled,
  withRenamedTitle,
} from "./reducer";
import type { ChatMessage } from "../chat/types";
import type { Conversation } from "./types";

function user(content: string): ChatMessage {
  return {
    id: `user-${content}`,
    role: "user",
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

function conversation(id: string, updatedAt: number, favorite = false): Conversation {
  return { ...createConversation(id, [user(`hello-${id}`)], "m", false), updatedAt, favorite };
}

describe("titleFromMessages", () => {
  it("uses the first user message, squashed and capped", () => {
    expect(titleFromMessages([user("  多\n行   标题  ")])).toBe("多 行 标题");
    expect(titleFromMessages([user("x".repeat(40))])).toBe(`${"x".repeat(28)}...`);
    expect(titleFromMessages([])).toBe("新对话");
  });
});

describe("sortConversations", () => {
  it("pins favorites ahead of newer non-favorites and caps the list", () => {
    const sorted = sortConversations([
      conversation("new", 300),
      conversation("fav-old", 100, true),
      conversation("mid", 200),
    ]);
    expect(sorted.map((item) => item.id)).toEqual(["fav-old", "new", "mid"]);
    const many = Array.from({ length: 70 }, (_, index) => conversation(`c${index}`, index));
    expect(sortConversations(many)).toHaveLength(60);
  });
});

describe("withRenamedTitle", () => {
  it("sets a custom title and blocks auto-title overwrite", () => {
    const renamed = withRenamedTitle(conversation("c", 1), "  我的标题  ", 50);
    expect(renamed).toMatchObject({ title: "我的标题", customTitle: true, updatedAt: 50 });
    const replaced = replaceConversationMessages(renamed, [user("别的内容")]);
    expect(replaced.title).toBe("我的标题");
  });

  it("ignores blank titles", () => {
    const original = conversation("c", 1);
    expect(withRenamedTitle(original, "   ", 50)).toBe(original);
  });
});

describe("withFavoriteToggled", () => {
  it("flips the flag and bumps updatedAt", () => {
    const toggled = withFavoriteToggled(conversation("c", 1), 99);
    expect(toggled).toMatchObject({ favorite: true, updatedAt: 99 });
    expect(withFavoriteToggled(toggled, 100).favorite).toBe(false);
  });
});
