// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { RouteErrorBoundary } from "./RouteErrorBoundary";

function BrokenRoute(): never {
  throw new Error("Trace chunk failed to load");
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RouteErrorBoundary", () => {
  it("contains route render failures and offers recovery actions", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(
      <MemoryRouter>
        <RouteErrorBoundary><BrokenRoute /></RouteErrorBoundary>
      </MemoryRouter>,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Trace chunk failed to load");
    expect(screen.getByRole("link", { name: "Return to chat" }).getAttribute("href")).toBe("/");
    expect(screen.getByRole("button", { name: "Reload page" })).toBeTruthy();
  });
});
