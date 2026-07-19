// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { getTrace } from "../../api/traceApi";
import { App } from "../../app/App";
import { NotFoundPage } from "../../app/NotFoundPage";

vi.mock("../../api/traceApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/traceApi")>();
  return { ...actual, getTrace: vi.fn() };
});

const getTraceMock = vi.mocked(getTrace);

beforeEach(() => {
  getTraceMock.mockReset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("TracePage", () => {
  it("renders the routed trace workspace and shared views", async () => {
    getTraceMock.mockResolvedValue({
      traceId: "trace-1",
      title: "Agent research run",
      kind: "agent",
      status: "completed",
      startedAt: "2026-07-19T10:00:00Z",
      completedAt: "2026-07-19T10:00:01Z",
      durationMs: 1500,
      error: "",
      summary: { spanCount: 2, totalTokens: 42, slowestSpan: "deepseek", slowestDurationMs: 1200 },
      spans: [
        { spanId: "root", parentSpanId: "", name: "agent", kind: "agent", status: "ok", offsetMs: 0, durationMs: 1500, totalTokens: 0, cacheHitRate: null, cacheHit: false, error: "" },
        { spanId: "llm", parentSpanId: "root", name: "deepseek", kind: "llm", status: "ok", offsetMs: 100, durationMs: 1200, totalTokens: 42, cacheHitRate: null, cacheHit: false, error: "" },
      ],
    });

    render(<MemoryRouter initialEntries={["/trace/trace-1"]}><App /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "Agent research run" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Span tree" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Waterfall" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Category summary" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Errors" })).toBeTruthy();
    expect(screen.getByText("42", { selector: ".trace-summary-item dd" })).toBeTruthy();
  });

  it("surfaces trace loading failures", async () => {
    getTraceMock.mockRejectedValue(new Error("Trace not found"));
    render(<MemoryRouter initialEntries={["/trace/missing"]}><App /></MemoryRouter>);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Trace not found");
    expect(alert.textContent).toContain("missing");
  });

  it("aborts the trace request when the route unmounts", async () => {
    let signal: AbortSignal | undefined;
    getTraceMock.mockImplementation((_traceId, options) => {
      signal = options?.signal;
      return new Promise(() => undefined);
    });
    const view = render(<MemoryRouter initialEntries={["/trace/slow"]}><App /></MemoryRouter>);

    await waitFor(() => expect(signal).toBeDefined());
    expect(signal?.aborted).toBe(false);
    view.unmount();
    expect(signal?.aborted).toBe(true);
  });

  it("uses the fallback route and returns to chat navigation", async () => {
    const user = userEvent.setup();
    const view = render(<MemoryRouter initialEntries={["/unknown"]}><App /></MemoryRouter>);
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeTruthy();
    view.unmount();
    render(
      <MemoryRouter initialEntries={["/unknown"]}>
        <Routes>
          <Route path="/" element={<p>Chat route</p>} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("link", { name: "Return to chat" }));
    expect(screen.getByText("Chat route")).toBeTruthy();
  });
});
