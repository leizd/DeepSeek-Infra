import { describe, expect, it } from "vitest";

import { ApiError } from "../api/httpClient";
import { shouldRetryQuery } from "./queryClient";

describe("shouldRetryQuery", () => {
  it("does not retry abort errors", () => {
    expect(shouldRetryQuery(0, new DOMException("aborted", "AbortError"))).toBe(false);
    expect(shouldRetryQuery(0, Object.assign(new Error("aborted"), { name: "AbortError" }))).toBe(false);
  });

  it("does not retry client errors", () => {
    for (const status of [400, 401, 403, 404, 409, 415, 422]) {
      expect(shouldRetryQuery(0, new ApiError("bad", status))).toBe(false);
    }
  });

  it("retries transient statuses once", () => {
    for (const status of [408, 425, 429, 500, 502, 503]) {
      expect(shouldRetryQuery(0, new ApiError("temporary", status))).toBe(true);
    }
    expect(shouldRetryQuery(1, new ApiError("temporary", 503))).toBe(false);
  });

  it("retries network failures once", () => {
    expect(shouldRetryQuery(0, new TypeError("Failed to fetch"))).toBe(true);
    expect(shouldRetryQuery(1, new TypeError("Failed to fetch"))).toBe(false);
  });
});
