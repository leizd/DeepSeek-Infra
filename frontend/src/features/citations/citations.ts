import type { JsonRecord, SearchSnapshot } from "../../domain/chat/types";

export interface SearchResult {
  title: string;
  url: string;
  snippet: string;
  citationId: string;
  round?: number;
}

export interface SearchRound {
  round: number;
  status: string;
  query: string;
  answer: string;
  error: string;
  results: SearchResult[];
}

function asRecord(value: unknown): JsonRecord | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonRecord) : null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function normalizeSearchResult(raw: unknown, fallbackIndex: number): SearchResult | null {
  const record = asRecord(raw);
  if (!record) return null;
  return {
    title: asString(record.title),
    url: asString(record.url),
    snippet: asString(record.snippet),
    citationId: asString(record.citation_id) || `W${fallbackIndex + 1}`,
    round: typeof record.round === "number" ? record.round : undefined,
  };
}

export function searchRounds(search: SearchSnapshot | null | undefined): SearchRound[] {
  const root = asRecord(search);
  if (!root) return [];
  const rounds = Array.isArray(root.rounds) ? root.rounds : [];
  const normalized = rounds.flatMap((raw, index) => {
    const record = asRecord(raw);
    if (!record) return [];
    return [{
      round: typeof record.round === "number" ? record.round : index + 1,
      status: asString(record.status) || "done",
      query: asString(record.query),
      answer: asString(record.answer),
      error: asString(record.error),
      results: Array.isArray(record.results)
        ? record.results.flatMap((result, resultIndex) => {
            const normalizedResult = normalizeSearchResult(result, resultIndex);
            return normalizedResult ? [normalizedResult] : [];
          })
        : [],
    }];
  });
  if (normalized.length) return normalized;
  return [{
    round: 1,
    status: asString(root.status) || "done",
    query: asString(root.query),
    answer: asString(root.answer),
    error: asString(root.error),
    results: Array.isArray(root.results)
      ? root.results.flatMap((result, resultIndex) => {
          const normalizedResult = normalizeSearchResult(result, resultIndex);
          return normalizedResult ? [normalizedResult] : [];
        })
      : [],
  }];
}

export function searchResults(search: SearchSnapshot | null | undefined): SearchResult[] {
  const root = asRecord(search);
  const direct = root && Array.isArray(root.results) && root.results.length
    ? root.results.flatMap((result, index) => {
        const normalized = normalizeSearchResult(result, index);
        return normalized ? [normalized] : [];
      })
    : [];
  if (direct.length) return direct;
  const results: SearchResult[] = [];
  const seen = new Set<string>();
  for (const round of searchRounds(search)) {
    for (const result of round.results) {
      const key = result.url || result.title;
      if (!key || seen.has(key)) continue;
      seen.add(key);
      results.push({ ...result, round: round.round });
    }
  }
  return results;
}

export function webCitationResults(search: SearchSnapshot | null | undefined): SearchResult[] {
  const results: SearchResult[] = [];
  const seen = new Set<string>();
  const append = (result: SearchResult) => {
    const key = result.url.trim();
    if (!key || seen.has(key)) return;
    seen.add(key);
    results.push(result);
  };
  for (const result of searchResults(search)) append(result);
  for (const round of searchRounds(search)) {
    for (const result of round.results) append(result);
  }
  return results;
}

export function isWebCitationId(citationId: string): boolean {
  return /^W\d+$/i.test(citationId.trim());
}

export function resolveWebCitationUrl(search: SearchSnapshot | null | undefined, citationId: string): string | null {
  const match = citationId.trim().match(/^W(\d+)$/i);
  if (!match) return null;
  const results = webCitationResults(search);
  const byId = results.find((result) => result.citationId.toLowerCase() === citationId.trim().toLowerCase());
  const target = byId ?? results[Number(match[1]) - 1];
  return target?.url || null;
}

export interface FileCitation {
  fileIndex: number;
  chunkIndex: number;
}

export function parseFileCitation(citationId: string): FileCitation | null {
  const match = citationId.trim().match(/^F(\d+)-(\d+)$/i);
  if (!match) return null;
  return { fileIndex: Number(match[1]), chunkIndex: Number(match[2]) };
}

export function chunkWindowStart(chunkIndex: number, windowSize: number): number {
  const safeIndex = Math.max(1, Math.round(chunkIndex));
  const safeWindow = Math.max(1, Math.round(windowSize));
  return Math.floor((safeIndex - 1) / safeWindow) * safeWindow + 1;
}
