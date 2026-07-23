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

export type FeatureRecoveryState = "initial" | "retry-available" | "reload-required";

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

export function createWorkspaceFeatureRegistry(
  loaders: WorkspaceFeatureLoaders,
  retryLoaders: WorkspaceFeatureLoaders = loaders,
) {
  const pending = new Map<WorkspaceFeature, Promise<WorkspaceFeatureModule>>();
  const retryGenerations = new Map<WorkspaceFeature, 0 | 1>();
  const recoveryStates = new Map<WorkspaceFeature, FeatureRecoveryState>();

  function load(feature: WorkspaceFeature): Promise<WorkspaceFeatureModule> {
    const existing = pending.get(feature);
    if (existing) return existing;
    const generation = retryGenerations.get(feature) ?? 0;
    const selectedLoaders = generation === 0 ? loaders : retryLoaders;
    const request = selectedLoaders[feature]();
    pending.set(feature, request);
    void request.catch(() => {
      if (pending.get(feature) === request) {
        pending.delete(feature);
        recoveryStates.set(feature, generation === 0 ? "retry-available" : "reload-required");
      }
    });
    return request;
  }

  function retry(feature: WorkspaceFeature): boolean {
    if (recoveryStates.get(feature) !== "retry-available") return false;
    pending.delete(feature);
    retryGenerations.set(feature, 1);
    recoveryStates.set(feature, "initial");
    return true;
  }

  return {
    load,
    preload(feature: WorkspaceFeature): Promise<void> {
      return load(feature).then(
        () => undefined,
        () => {
          retry(feature);
        },
      );
    },
    retry,
    recoveryState(feature: WorkspaceFeature): FeatureRecoveryState {
      return recoveryStates.get(feature) ?? "initial";
    },
  };
}

const registry = createWorkspaceFeatureRegistry(
  workspaceFeatureLoaders,
  workspaceFeatureRetryLoaders,
);

let skillsRuntimePromise: Promise<{ default: ComponentType<PropsWithChildren> }> | null = null;
let skillsRuntimeRetryGeneration: 0 | 1 = 0;
let skillsRuntimeRecoveryState: FeatureRecoveryState = "initial";

export function loadWorkspaceSkillsRuntime(): Promise<{ default: ComponentType<PropsWithChildren> }> {
  if (skillsRuntimePromise) return skillsRuntimePromise;
  const request = skillsRuntimeRetryGeneration === 0
    ? import("../skills/SkillsRuntimeBoundary")
    : import("../skills/SkillsRuntimeBoundary?workspace-retry");
  skillsRuntimePromise = request;
  void request.catch(() => {
    if (skillsRuntimePromise === request) {
      skillsRuntimePromise = null;
      skillsRuntimeRecoveryState = skillsRuntimeRetryGeneration === 0
        ? "retry-available"
        : "reload-required";
    }
  });
  return request;
}

export function workspaceSkillsRuntimeRecoveryState(): FeatureRecoveryState {
  return skillsRuntimeRecoveryState;
}

export function retryWorkspaceSkillsRuntime(): boolean {
  if (skillsRuntimeRecoveryState !== "retry-available") return false;
  skillsRuntimePromise = null;
  skillsRuntimeRetryGeneration = 1;
  skillsRuntimeRecoveryState = "initial";
  return true;
}

function preloadWorkspaceSkillsRuntime(): Promise<void> {
  return loadWorkspaceSkillsRuntime().then(
    () => undefined,
    () => {
      retryWorkspaceSkillsRuntime();
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
export function workspaceFeatureRecoveryState(feature: WorkspaceFeature): FeatureRecoveryState {
  return registry.recoveryState(feature);
}

export function retryWorkspaceFeature(feature: WorkspaceFeature): boolean {
  return registry.retry(feature);
}
