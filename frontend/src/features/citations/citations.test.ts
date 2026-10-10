import { describe, expect, it } from "vitest";

import {
  chunkWindowStart,
  isWebCitationId,
  parseFileCitation,
  resolveWebCitationUrl,
  searchResults,
  searchRounds,
  webCitationResults,
} from "./citations";

const multiRoundSearch = {
  status: "done",
  rounds: [
    { round: 1, status: "done", query: "q1", results: [
      { title: "A", url: "https://a.example", citation_id: "W1" },
      { title: "B", url: "https://b.example" },
    ] },
    { round: 2, status: "done", query: "q2", results: [
      { title: "A2", url: "https://a.example" },
      { title: "C", url: "https://c.example", citation_id: "W9" },
    ] },
  ],
};

describe("searchRounds", () => {
  it("normalizes explicit rounds and falls back to a single synthetic round", () => {
    expect(searchRounds(multiRoundSearch)).toHaveLength(2);
    const fallback = searchRounds({ status: "done", query: "q", results: [{ title: "A", url: "https://a.example" }] });
    expect(fallback).toHaveLength(1);
    expect(fallback[0]).toMatchObject({ round: 1, query: "q" });
    expect(fallback[0].results[0]).toMatchObject({ citationId: "W1" });
    expect(searchRounds(null)).toEqual([]);
  });
});

describe("searchResults", () => {
  it("dedupes across rounds by url and tags the round", () => {
    const results = searchResults(multiRoundSearch);
    expect(results.map((result) => result.url)).toEqual(["https://a.example", "https://b.example", "https://c.example"]);
    expect(results[2]).toMatchObject({ round: 2 });
  });

  it("prefers top-level results when present", () => {
    const results = searchResults({ results: [{ title: "T", url: "https://t.example" }], rounds: [] });
    expect(results).toHaveLength(1);
  });
});

describe("webCitationResults / resolveWebCitationUrl", () => {
  it("resolves by citation_id first, then by ordinal", () => {
    expect(resolveWebCitationUrl(multiRoundSearch, "W9")).toBe("https://c.example");
    expect(resolveWebCitationUrl(multiRoundSearch, "w1")).toBe("https://a.example");
    expect(resolveWebCitationUrl(multiRoundSearch, "W2")).toBe("https://b.example");
  });

  it("returns null for unknown or malformed citations", () => {
    expect(resolveWebCitationUrl(multiRoundSearch, "W99")).toBeNull();
    expect(resolveWebCitationUrl(multiRoundSearch, "F1-2")).toBeNull();
    expect(resolveWebCitationUrl(null, "W1")).toBeNull();
  });

  it("dedupes url-less results out", () => {
    expect(webCitationResults({ results: [{ title: "no-url" }] })).toEqual([]);
  });

  it("matches the web citation id shape", () => {
    expect(isWebCitationId("W12")).toBe(true);
    expect(isWebCitationId(" w3 ")).toBe(true);
    expect(isWebCitationId("F1-2")).toBe(false);
  });
});

describe("parseFileCitation / chunkWindowStart", () => {
  it("parses F<file>-<chunk> ids", () => {
    expect(parseFileCitation("F2-7")).toEqual({ fileIndex: 2, chunkIndex: 7 });
    expect(parseFileCitation("f1-12")).toEqual({ fileIndex: 1, chunkIndex: 12 });
    expect(parseFileCitation("W3")).toBeNull();
    expect(parseFileCitation("F1")).toBeNull();
  });

  it("maps a chunk index to its reader window start", () => {
    expect(chunkWindowStart(1, 6)).toBe(1);
    expect(chunkWindowStart(6, 6)).toBe(1);
    expect(chunkWindowStart(7, 6)).toBe(7);
    expect(chunkWindowStart(0, 6)).toBe(1);
  });
});
