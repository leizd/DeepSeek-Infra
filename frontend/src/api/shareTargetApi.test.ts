import { describe, expect, it, vi } from "vitest";

import { HttpClient } from "./httpClient";
import { getShareTarget, normalizeShareTarget, readShareIdFromLocation, stripShareIdFromLocation } from "./shareTargetApi";

describe("shareTargetApi", () => {
  it("normalizes prompt, attachments and errors", () => {
    const share = normalizeShareTarget({
      prompt: "  帮我看看这篇文章  ",
      attachments: [{ name: "a.pdf", fileId: "f1", kind: "pdf", text: "正文" }],
      errors: [{ error: "b.bin 无法识别" }, { noError: true }],
    });
    expect(share.prompt).toBe("帮我看看这篇文章");
    expect(share.attachments[0]).toMatchObject({ name: "a.pdf", fileId: "f1", kind: "pdf" });
    expect(share.errors).toEqual(["b.bin 无法识别"]);
    expect(normalizeShareTarget(null)).toEqual({ prompt: "", attachments: [], errors: [] });
  });

  it("fetches and normalizes the share payload", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ share: { prompt: "hi" } }), { status: 200 }));
    const share = await getShareTarget("s-1", new HttpClient({ fetchImpl }));
    expect(String((fetchImpl.mock.calls[0] as unknown as [string])[0])).toBe("/api/share-target?id=s-1");
    expect(share.prompt).toBe("hi");
  });

  it("reads and strips the share id from the location", () => {
    expect(readShareIdFromLocation("?share=abc&x=1")).toBe("abc");
    expect(readShareIdFromLocation("?x=1")).toBe("");
    expect(stripShareIdFromLocation("/ui/", "?share=abc&x=1", "#top")).toBe("/ui/?x=1#top");
    expect(stripShareIdFromLocation("/ui/", "?share=abc", "")).toBe("/ui/");
  });
});
