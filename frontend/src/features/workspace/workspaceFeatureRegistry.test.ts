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

  it("uses one unique retry import and then requires reload", async () => {
    const initial = vi.fn(() => Promise.reject(new Error("initial chunk offline")));
    const retry = vi.fn(() => Promise.reject(new Error("retry chunk offline")));
    const registry = createWorkspaceFeatureRegistry(loadersWith(initial), loadersWith(retry));

    await expect(registry.load("memory")).rejects.toThrow("initial chunk offline");
    expect(registry.recoveryState("memory")).toBe("retry-available");
    expect(registry.retry("memory")).toBe(true);
    await expect(registry.load("memory")).rejects.toThrow("retry chunk offline");
    expect(registry.recoveryState("memory")).toBe("reload-required");
    expect(registry.retry("memory")).toBe(false);
    expect(initial).toHaveBeenCalledTimes(1);
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("does not consume one feature retry when another feature fails", async () => {
    const initial = vi.fn(() => Promise.reject(new Error("chunk offline")));
    const retry = vi.fn(() => Promise.resolve({ default: () => null }));
    const registry = createWorkspaceFeatureRegistry(loadersWith(initial), loadersWith(retry));

    await expect(registry.load("projects")).rejects.toThrow("chunk offline");
    expect(registry.retry("projects")).toBe(true);
    expect(registry.recoveryState("skills")).toBe("initial");
    expect(registry.retry("skills")).toBe(false);
  });
});
