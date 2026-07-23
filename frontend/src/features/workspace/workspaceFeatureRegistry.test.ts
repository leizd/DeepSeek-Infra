import { describe, expect, it, vi } from "vitest";

import {
  createWorkspaceFeatureRegistry,
  type WorkspaceFeature,
  type WorkspaceFeatureLoaders,
  type WorkspaceFeatureModule,
} from "./workspaceFeatureRegistry";

const FEATURES: WorkspaceFeature[] = [
  "settings",
  "projects",
  "skills",
  "memory",
  "reminders",
  "diagnostics",
  "file-preview",
  "image-lightbox",
  "activity",
];

function loadersWith(loader: () => Promise<WorkspaceFeatureModule>): WorkspaceFeatureLoaders {
  return Object.fromEntries(FEATURES.map((feature) => [feature, loader])) as WorkspaceFeatureLoaders;
}

describe("workspace feature registry", () => {
  it("deduplicates imports across preload and activation", async () => {
    const module = { default: () => null };
    const loader = vi.fn(() => Promise.resolve(module));
    const registry = createWorkspaceFeatureRegistry(loadersWith(loader));

    const preload = registry.preload("projects");
    const first = registry.load("projects");
    const second = registry.load("projects");

    await expect(Promise.all([preload, first, second])).resolves.toBeDefined();
    expect(loader).toHaveBeenCalledTimes(1);
    expect(first).toBe(second);
  });

  it("does not share imports between different feature identities", async () => {
    const loader = vi.fn(() => Promise.resolve({ default: () => null }));
    const registry = createWorkspaceFeatureRegistry(loadersWith(loader));
    await Promise.all([registry.load("projects"), registry.load("skills")]);
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("removes a rejected preload so activation can retry", async () => {
    const loader = vi
      .fn<() => Promise<WorkspaceFeatureModule>>()
      .mockRejectedValueOnce(new Error("chunk offline"))
      .mockResolvedValueOnce({ default: () => null });
    const registry = createWorkspaceFeatureRegistry(loadersWith(loader));

    await expect(registry.preload("skills")).resolves.toBeUndefined();
    await expect(registry.load("skills")).resolves.toBeDefined();
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("creates a new import after an explicit retry", async () => {
    const loader = vi.fn(() => Promise.resolve({ default: () => null }));
    const registry = createWorkspaceFeatureRegistry(loadersWith(loader));
    await registry.load("memory");
    registry.retry("memory");
    await registry.load("memory");
    expect(loader).toHaveBeenCalledTimes(2);
  });
});
