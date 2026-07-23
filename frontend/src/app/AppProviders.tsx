import { QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";

import { AttachmentsProvider } from "../contexts/AttachmentsContext";
import { ActivityProvider } from "../contexts/ActivityContext";
import { ChatProvider } from "../contexts/ChatContext";
import { DiagnosticsProvider } from "../contexts/DiagnosticsContext";
import { FilePreviewProvider } from "../contexts/FilePreviewContext";
import { MemoryProvider } from "../contexts/MemoryContext";
import { OverlayProvider } from "../contexts/OverlayContext";
import { ProjectsProvider } from "../contexts/ProjectsContext";
import { SettingsProvider } from "../contexts/SettingsContext";
import { queryClient } from "./queryClient";

export function AppProviders({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider client={queryClient}>
      <SettingsProvider>
        <OverlayProvider>
          <ProjectsProvider>
            <MemoryProvider>
              <ChatProvider>
                <AttachmentsProvider>
                  <FilePreviewProvider>
                    <ActivityProvider>
                      <DiagnosticsProvider>{children}</DiagnosticsProvider>
                    </ActivityProvider>
                  </FilePreviewProvider>
                </AttachmentsProvider>
              </ChatProvider>
            </MemoryProvider>
          </ProjectsProvider>
        </OverlayProvider>
      </SettingsProvider>
    </QueryClientProvider>
  );
}
