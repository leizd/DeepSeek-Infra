export const appRoutes = {
  root: "/",
  preview: "/ui/",
  trace: (traceId: string) => `/trace/${encodeURIComponent(traceId)}`,
} as const;
