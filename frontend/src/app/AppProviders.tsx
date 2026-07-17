import type { PropsWithChildren } from "react";

import { ChatProvider } from "../contexts/ChatContext";
import { OverlayProvider } from "../contexts/OverlayContext";
import { SettingsProvider } from "../contexts/SettingsContext";

export function AppProviders({ children }: PropsWithChildren) {
  return (
    <SettingsProvider>
      <OverlayProvider>
        <ChatProvider>{children}</ChatProvider>
      </OverlayProvider>
    </SettingsProvider>
  );
}
