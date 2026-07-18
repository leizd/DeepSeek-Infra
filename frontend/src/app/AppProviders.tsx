import type { PropsWithChildren } from "react";

import { AttachmentsProvider } from "../contexts/AttachmentsContext";
import { ActivityProvider } from "../contexts/ActivityContext";
import { ChatProvider } from "../contexts/ChatContext";
import { FilePreviewProvider } from "../contexts/FilePreviewContext";
import { OverlayProvider } from "../contexts/OverlayContext";
import { SettingsProvider } from "../contexts/SettingsContext";

export function AppProviders({ children }: PropsWithChildren) {
  return (
    <SettingsProvider>
      <OverlayProvider>
        <ChatProvider>
          <AttachmentsProvider>
            <FilePreviewProvider>
              <ActivityProvider>{children}</ActivityProvider>
            </FilePreviewProvider>
          </AttachmentsProvider>
        </ChatProvider>
      </OverlayProvider>
    </SettingsProvider>
  );
}
