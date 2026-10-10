import { createContext, useContext, useMemo, useState, type PropsWithChildren } from "react";

export type DiagnosticsMode = "rows" | "trace";

export interface DiagnosticsTarget {
  messageId: string;
  mode: DiagnosticsMode;
}

interface DiagnosticsContextValue {
  target: DiagnosticsTarget | null;
  openDiagnostics(messageId: string, mode?: DiagnosticsMode): void;
  closeDiagnostics(): void;
}

const DiagnosticsContext = createContext<DiagnosticsContextValue | null>(null);

export function DiagnosticsProvider({ children }: PropsWithChildren) {
  const [target, setTarget] = useState<DiagnosticsTarget | null>(null);
  const value = useMemo<DiagnosticsContextValue>(
    () => ({
      target,
      openDiagnostics: (messageId, mode = "rows") => setTarget({ messageId, mode }),
      closeDiagnostics: () => setTarget(null),
    }),
    [target],
  );
  return <DiagnosticsContext.Provider value={value}>{children}</DiagnosticsContext.Provider>;
}

export function useDiagnostics(): DiagnosticsContextValue {
  const value = useContext(DiagnosticsContext);
  if (!value) throw new Error("useDiagnostics must be used inside DiagnosticsProvider");
  return value;
}
