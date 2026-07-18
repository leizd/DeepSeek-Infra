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
import { SkillsProvider } from "../contexts/SkillsContext";

export function AppProviders({ children }: PropsWithChildren) {
  return (
    <SettingsProvider>
      <OverlayProvider>
        <ProjectsProvider>
          <SkillsProvider>
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
          </SkillsProvider>
        </ProjectsProvider>
      </OverlayProvider>
    </SettingsProvider>
  );
}
