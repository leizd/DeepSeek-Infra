import type { ComponentType, PropsWithChildren } from "react";

export type WorkspaceFeature =
  | "settings"
  | "projects"
  | "skills"
  | "memory"
  | "reminders"
  | "diagnostics"
  | "file-preview"
  | "image-lightbox"
  | "activity";

export interface WorkspaceFeatureModule {
  default: ComponentType;
}

export type WorkspaceFeatureLoaders = Record<
  WorkspaceFeature,
  () => Promise<WorkspaceFeatureModule>
>;

export const workspaceFeatureModuleIds = {
  settings: "src/features/settings/ConnectionSettingsFeature.tsx",
  projects: "src/features/projects/ProjectsFeature.tsx",
  skills: "src/features/skills/SkillsFeature.tsx",
  memory: "src/features/memory/MemoryFeature.tsx",
  reminders: "src/features/reminders/RemindersFeature.tsx",
  diagnostics: "src/features/diagnostics/DiagnosticsFeature.tsx",
  "file-preview": "src/features/file-reader/FilePreviewFeature.tsx",
  "image-lightbox": "src/features/file-reader/ImageLightboxFeature.tsx",
  activity: "src/features/activity/ActivityFeature.tsx",
} satisfies Record<WorkspaceFeature, string>;

export const workspaceFeatureLoaders = {
  settings: () => import("../settings/ConnectionSettingsFeature"),
  projects: () => import("../projects/ProjectsFeature"),
  skills: () => import("../skills/SkillsFeature"),
  memory: () => import("../memory/MemoryFeature"),
  reminders: () => import("../reminders/RemindersFeature"),
  diagnostics: () => import("../diagnostics/DiagnosticsFeature"),
  "file-preview": () => import("../file-reader/FilePreviewFeature"),
  "image-lightbox": () => import("../file-reader/ImageLightboxFeature"),
  activity: () => import("../activity/ActivityFeature"),
} satisfies WorkspaceFeatureLoaders;

const workspaceFeatureRetryLoaders = {
  settings: () => import("../settings/ConnectionSettingsFeature?workspace-retry"),
  projects: () => import("../projects/ProjectsFeature?workspace-retry"),
  skills: () => import("../skills/SkillsFeature?workspace-retry"),
  memory: () => import("../memory/MemoryFeature?workspace-retry"),
  reminders: () => import("../reminders/RemindersFeature?workspace-retry"),
  diagnostics: () => import("../diagnostics/DiagnosticsFeature?workspace-retry"),
  "file-preview": () => import("../file-reader/FilePreviewFeature?workspace-retry"),
  "image-lightbox": () => import("../file-reader/ImageLightboxFeature?workspace-retry"),
  activity: () => import("../activity/ActivityFeature?workspace-retry"),
} satisfies WorkspaceFeatureLoaders;

const workspaceFeatureRecoveryLoaders = {
  settings: () => import("../settings/ConnectionSettingsFeature?workspace-recovery"),
  projects: () => import("../projects/ProjectsFeature?workspace-recovery"),
  skills: () => import("../skills/SkillsFeature?workspace-recovery"),
  memory: () => import("../memory/MemoryFeature?workspace-recovery"),
  reminders: () => import("../reminders/RemindersFeature?workspace-recovery"),
  diagnostics: () => import("../diagnostics/DiagnosticsFeature?workspace-recovery"),
  "file-preview": () => import("../file-reader/FilePreviewFeature?workspace-recovery"),
  "image-lightbox": () => import("../file-reader/ImageLightboxFeature?workspace-recovery"),
  activity: () => import("../activity/ActivityFeature?workspace-recovery"),
} satisfies WorkspaceFeatureLoaders;

export function createWorkspaceFeatureRegistry(
  loaders: WorkspaceFeatureLoaders,
  retryLoaders: WorkspaceFeatureLoaders = loaders,
  recoveryLoaders: WorkspaceFeatureLoaders = retryLoaders,
) {
  const pending = new Map<WorkspaceFeature, Promise<WorkspaceFeatureModule>>();
  const attempts = new Map<WorkspaceFeature, number>();

  function load(feature: WorkspaceFeature): Promise<WorkspaceFeatureModule> {
    const existing = pending.get(feature);
    if (existing) return existing;
    const attempt = attempts.get(feature) ?? 0;
    const selectedLoaders = attempt === 0 ? loaders : attempt === 1 ? retryLoaders : recoveryLoaders;
    const request = selectedLoaders[feature]();
    pending.set(feature, request);
    void request.catch(() => {
      if (pending.get(feature) === request) {
        pending.delete(feature);
      }
    });
    return request;
  }

  return {
    load,
    preload(feature: WorkspaceFeature): Promise<void> {
      return load(feature).then(
        () => undefined,
        () => {
          attempts.set(feature, (attempts.get(feature) ?? 0) + 1);
        },
      );
    },
    retry(feature: WorkspaceFeature): void {
      pending.delete(feature);
      attempts.set(feature, (attempts.get(feature) ?? 0) + 1);
    },
  };
}

const registry = createWorkspaceFeatureRegistry(
  workspaceFeatureLoaders,
  workspaceFeatureRetryLoaders,
  workspaceFeatureRecoveryLoaders,
);

let skillsRuntimePromise: Promise<{ default: ComponentType<PropsWithChildren> }> | null = null;
let skillsRuntimeRetryGeneration = 0;

export function loadWorkspaceSkillsRuntime(): Promise<{ default: ComponentType<PropsWithChildren> }> {
  if (skillsRuntimePromise) return skillsRuntimePromise;
  const request = skillsRuntimeRetryGeneration === 0
    ? import("../skills/SkillsRuntimeBoundary")
    : skillsRuntimeRetryGeneration === 1
      ? import("../skills/SkillsRuntimeBoundary?workspace-retry")
      : import("../skills/SkillsRuntimeBoundary?workspace-recovery");
  skillsRuntimePromise = request;
  void request.catch(() => {
    if (skillsRuntimePromise === request) {
      skillsRuntimePromise = null;
    }
  });
  return request;
}

export function retryWorkspaceSkillsRuntime(): void {
  skillsRuntimePromise = null;
  skillsRuntimeRetryGeneration += 1;
}

function preloadWorkspaceSkillsRuntime(): Promise<void> {
  return loadWorkspaceSkillsRuntime().then(
    () => undefined,
    () => {
      skillsRuntimeRetryGeneration += 1;
    },
  );
}

export const loadWorkspaceFeature = registry.load;
export function preloadWorkspaceFeature(feature: WorkspaceFeature): Promise<void> {
  const loads: Promise<unknown>[] = [registry.preload(feature)];
  if (feature === "projects" || feature === "skills") loads.push(preloadWorkspaceSkillsRuntime());
  return Promise.all(loads).then(
    () => undefined,
    () => undefined,
  );
}
export function retryWorkspaceFeature(feature: WorkspaceFeature): void {
  registry.retry(feature);
  if (feature === "projects" || feature === "skills") retryWorkspaceSkillsRuntime();
}
