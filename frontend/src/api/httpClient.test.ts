import { describe, expect, it, vi } from "vitest";

import { HttpClient } from "./httpClient";

describe("HttpClient", () => {
  it("normalizes JSON API errors", async () => {
    const client = new HttpClient({
      fetchImpl: async () =>
        new Response(JSON.stringify({ error: "denied", code: "forbidden" }), {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }),
    });

    await expect(client.request("/api/private")).rejects.toMatchObject({
      name: "ApiError",
      message: "denied",
      status: 403,
      code: "forbidden",
    });
  });

  it("adds injected bearer auth without reading persistent browser storage", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      expect(headers.get("Authorization")).toBe("Bearer session-token");
      expect(init?.credentials).toBe("same-origin");
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    const client = new HttpClient({ fetchImpl, getAuthToken: () => "session-token" });

    await expect(client.json<{ ok: boolean }>("/api/config")).resolves.toEqual({ ok: true });
  });
});
