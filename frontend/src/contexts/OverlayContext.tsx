import { createContext, useContext, useMemo, useState, type PropsWithChildren } from "react";

export type OverlayName = "history" | "settings" | null;

interface OverlayContextValue {
  activeOverlay: OverlayName;
  openOverlay(name: Exclude<OverlayName, null>): void;
  closeOverlay(): void;
}

const OverlayContext = createContext<OverlayContextValue | null>(null);

export function OverlayProvider({ children }: PropsWithChildren) {
  const [activeOverlay, setActiveOverlay] = useState<OverlayName>(null);
  const value = useMemo(
    () => ({
      activeOverlay,
      openOverlay: (name: Exclude<OverlayName, null>) => setActiveOverlay(name),
      closeOverlay: () => setActiveOverlay(null),
    }),
    [activeOverlay],
  );
  return <OverlayContext.Provider value={value}>{children}</OverlayContext.Provider>;
}

export function useOverlay(): OverlayContextValue {
  const value = useContext(OverlayContext);
  if (!value) throw new Error("useOverlay must be used inside OverlayProvider");
  return value;
}
