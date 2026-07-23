import { lazy, Suspense, useEffect, useMemo, useState } from "react";

import { useActivity } from "../../contexts/ActivityContext";
import { useChat } from "../../contexts/ChatContext";
import { useDiagnostics } from "../../contexts/DiagnosticsContext";
import { useFilePreview } from "../../contexts/FilePreviewContext";
import { useOverlay, type OverlayName } from "../../contexts/OverlayContext";
import { messageHasActivity } from "../activity/activitySummary";
import { WorkspaceFeatureBoundary } from "./WorkspaceFeatureBoundary";
import {
  loadWorkspaceFeature,
  loadWorkspaceSkillsRuntime,
  retryWorkspaceFeature,
  retryWorkspaceSkillsRuntime,
  workspaceFeatureRecoveryState,
  workspaceSkillsRuntimeRecoveryState,
  type WorkspaceFeature,
} from "./workspaceFeatureRegistry";

const OVERLAY_FEATURES = new Set<WorkspaceFeature>(["settings", "projects", "skills", "memory", "reminders"]);

function WorkspaceDrawerLoading({ feature }: { feature: WorkspaceFeature }) {
  return (
    <section className="settings-drawer workspace-drawer workspace-drawer-loading" role="status" aria-live="polite">
      <span className="workspace-loading-spinner" aria-hidden="true" />
      <p>正在加载 {feature}…</p>
    </section>
  );
}

function WorkspaceFeatureSlot({
  feature,
  onClose,
}: {
  feature: WorkspaceFeature;
  onClose(): void;
}) {
  const [retryGeneration, setRetryGeneration] = useState(0);
  const LazyFeature = useMemo(
    () => lazy(() => loadWorkspaceFeature(feature)),
    [feature, retryGeneration],
  );

  return (
    <WorkspaceFeatureBoundary
      key={`${feature}:${retryGeneration}`}
      feature={feature}
      onClose={onClose}
      onReload={() => window.location.reload()}
      recoveryState={() => workspaceFeatureRecoveryState(feature)}
      onRetry={() => {
        if (retryWorkspaceFeature(feature)) {
          setRetryGeneration((current) => current + 1);
        }
      }}
    >
      <Suspense fallback={<WorkspaceDrawerLoading feature={feature} />}>
        <LazyFeature />
      </Suspense>
    </WorkspaceFeatureBoundary>
  );
}

function activeWorkspaceFeature(activeOverlay: OverlayName): WorkspaceFeature | null {
  return activeOverlay && OVERLAY_FEATURES.has(activeOverlay as WorkspaceFeature)
    ? activeOverlay as WorkspaceFeature
    : null;
}

export function WorkspaceOverlayHost() {
  const overlay = useOverlay();
  const feature = activeWorkspaceFeature(overlay.activeOverlay);
  const needsSkills = feature === "projects" || feature === "skills";
  const [runtimeRetryGeneration, setRuntimeRetryGeneration] = useState(0);
  const LazySkillsRuntimeBoundary = useMemo(
    () => lazy(() => loadWorkspaceSkillsRuntime()),
    [runtimeRetryGeneration],
  );
  if (!feature) return null;

  const slot = <WorkspaceFeatureSlot feature={feature} onClose={overlay.closeOverlay} />;
  if (!needsSkills) return slot;
  return (
    <WorkspaceFeatureBoundary
      key={`skills-runtime:${runtimeRetryGeneration}`}
      feature={feature}
      onClose={overlay.closeOverlay}
      onReload={() => window.location.reload()}
      recoveryState={workspaceSkillsRuntimeRecoveryState}
      onRetry={() => {
        if (retryWorkspaceSkillsRuntime()) {
          setRuntimeRetryGeneration((current) => current + 1);
        }
      }}
    >
      <Suspense fallback={<WorkspaceDrawerLoading feature={feature} />}>
        <LazySkillsRuntimeBoundary>{slot}</LazySkillsRuntimeBoundary>
      </Suspense>
    </WorkspaceFeatureBoundary>
  );
}

export function ContextualFeatureHost() {
  const activity = useActivity();
  const chat = useChat();
  const diagnostics = useDiagnostics();
  const preview = useFilePreview();
  const overlay = useOverlay();

  useEffect(() => {
    if (overlay.activeOverlay) return;
    if (typeof window !== "undefined" && window.innerWidth < 960) return;
    const candidate = [...chat.messages].reverse().find((message) => message.streaming && messageHasActivity(message));
    if (candidate) activity.autoOpen(candidate.id);
  }, [activity, chat.messages, overlay.activeOverlay]);

  return (
    <>
      {activity.openMessageId && (
        <WorkspaceFeatureSlot feature="activity" onClose={activity.closeActivity} />
      )}
      {diagnostics.target && (
        <WorkspaceFeatureSlot feature="diagnostics" onClose={diagnostics.closeDiagnostics} />
      )}
      {preview.state.attachment && (
        <WorkspaceFeatureSlot feature="file-preview" onClose={preview.close} />
      )}
      {preview.lightbox && (
        <WorkspaceFeatureSlot feature="image-lightbox" onClose={preview.closeLightbox} />
      )}
    </>
  );
}
