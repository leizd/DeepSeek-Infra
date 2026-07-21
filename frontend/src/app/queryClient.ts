import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/httpClient";

export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (failureCount >= 1) return false;
  if (error instanceof DOMException && error.name === "AbortError") return false;
  if (error instanceof Error && error.name === "AbortError") return false;
  if (error instanceof ApiError) {
    return error.status === 408 || error.status === 425 || error.status === 429 || error.status >= 500;
  }
  return true;
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: shouldRetryQuery,
      refetchOnWindowFocus: false,
    },
  },
});
