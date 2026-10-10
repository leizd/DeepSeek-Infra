// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
import type { ComponentType, PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OverlayName } from "../../contexts/OverlayContext";
import type {
  FeatureRecoveryState,
  WorkspaceFeature,
  WorkspaceFeatureModule,
} from "./workspaceFeatureRegistry";

const mocks = vi.hoisted(() => ({
  activeOverlay: null as OverlayName,
  loadFeature: vi.fn(),
  loadSkillsRuntime: vi.fn(),
  retryFeature: vi.fn(),
  retrySkillsRuntime: vi.fn(),
  featureRecoveryState: "retry-available" as FeatureRecoveryState,
  skillsRecoveryState: "retry-available" as FeatureRecoveryState,
}));

vi.mock("../../contexts/OverlayContext", () => ({
  useOverlay: () => ({
    get activeOverlay() {
      return mocks.activeOverlay;
    },
    openOverlay: vi.fn(),
    closeOverlay: vi.fn(() => {
      mocks.activeOverlay = null;
    }),
  }),
}));

vi.mock("../../contexts/ActivityContext", () => ({
  useActivity: () => ({ openMessageId: null, autoOpen: vi.fn(), closeActivity: vi.fn() }),
}));
vi.mock("../../contexts/ChatContext", () => ({ useChat: () => ({ messages: [] }) }));
vi.mock("../../contexts/DiagnosticsContext", () => ({
  useDiagnostics: () => ({ target: null, closeDiagnostics: vi.fn() }),
}));
vi.mock("../../contexts/FilePreviewContext", () => ({
  useFilePreview: () => ({ state: { attachment: null }, lightbox: null, close: vi.fn(), closeLightbox: vi.fn() }),
}));

vi.mock("./workspaceFeatureRegistry", () => ({
  loadWorkspaceFeature: (feature: WorkspaceFeature) => mocks.loadFeature(feature),
  loadWorkspaceSkillsRuntime: () => mocks.loadSkillsRuntime(),
  retryWorkspaceFeature: (feature: WorkspaceFeature) => mocks.retryFeature(feature),
  retryWorkspaceSkillsRuntime: () => mocks.retrySkillsRuntime(),
  workspaceFeatureRecoveryState: () => mocks.featureRecoveryState,
  workspaceSkillsRuntimeRecoveryState: () => mocks.skillsRecoveryState,
}));

import { WorkspaceOverlayHost } from "./WorkspaceFeatureHosts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function panel(name: string): WorkspaceFeatureModule {
  return { default: () => <div>{name}</div> };
}

beforeEach(() => {
  mocks.activeOverlay = null;
  mocks.loadFeature.mockReset();
  mocks.loadSkillsRuntime.mockReset();
  mocks.retryFeature.mockReset();
  mocks.retrySkillsRuntime.mockReset();
  mocks.retryFeature.mockReturnValue(true);
  mocks.retrySkillsRuntime.mockReturnValue(true);
  mocks.featureRecoveryState = "retry-available";
  mocks.skillsRecoveryState = "retry-available";
  mocks.loadSkillsRuntime.mockResolvedValue({
    default: ({ children }: PropsWithChildren) => children,
  } satisfies { default: ComponentType<PropsWithChildren> });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("WorkspaceOverlayHost", () => {
  it("does not load or mount the skills runtime on a cold start", () => {
    render(<WorkspaceOverlayHost />);
    expect(mocks.loadSkillsRuntime).not.toHaveBeenCalled();
    expect(mocks.loadFeature).not.toHaveBeenCalled();
  });

  it("keeps the latest overlay when an older chunk resolves late", async () => {
    const projects = deferred<WorkspaceFeatureModule>();
    const skills = deferred<WorkspaceFeatureModule>();
    mocks.loadFeature.mockImplementation((feature: WorkspaceFeature) =>
      feature === "projects" ? projects.promise : skills.promise,
    );
    mocks.activeOverlay = "projects";
    const view = render(<WorkspaceOverlayHost />);
    await act(async () => undefined);

    mocks.activeOverlay = "skills";
    view.rerender(<WorkspaceOverlayHost />);
    await act(async () => {
      skills.resolve(panel("Skills panel"));
      await skills.promise;
    });
    expect(await screen.findByText("Skills panel")).toBeTruthy();

    await act(async () => {
      projects.resolve(panel("Projects panel"));
      await projects.promise;
    });
    expect(screen.queryByText("Projects panel")).toBeNull();
    expect(screen.getByText("Skills panel")).toBeTruthy();
  });

  it("does not show a feature after the drawer closes during loading", async () => {
    const projects = deferred<WorkspaceFeatureModule>();
    mocks.loadFeature.mockReturnValue(projects.promise);
    mocks.activeOverlay = "projects";
    const view = render(<WorkspaceOverlayHost />);
    await act(async () => undefined);

    mocks.activeOverlay = null;
    view.rerender(<WorkspaceOverlayHost />);
    await act(async () => {
      projects.resolve(panel("Projects panel"));
      await projects.promise;
    });
    expect(screen.queryByText("Projects panel")).toBeNull();
  });

  it("contains a rejected chunk and opens after retry", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.activeOverlay = "memory";
    mocks.loadFeature
      .mockRejectedValueOnce(new Error("Failed to fetch dynamically imported module"))
      .mockResolvedValueOnce(panel("Memory panel"));
    const view = render(<WorkspaceOverlayHost />);

    expect(await screen.findByText("记忆面板加载失败")).toBeTruthy();
    await act(async () => {
      screen.getByRole("button", { name: "重试" }).click();
    });
    expect(await screen.findByText("Memory panel")).toBeTruthy();
    expect(mocks.retryFeature).toHaveBeenCalledWith("memory");
    view.unmount();
    errorSpy.mockRestore();
  });

  it("offers refresh instead of a fake third request when the retry import also fails", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.activeOverlay = "memory";
    mocks.loadFeature.mockRejectedValue(new Error("Failed to fetch dynamically imported module"));
    mocks.retryFeature.mockImplementation(() => {
      mocks.featureRecoveryState = "reload-required";
      return true;
    });
    render(<WorkspaceOverlayHost />);

    expect(await screen.findByRole("button", { name: "重试" })).toBeTruthy();
    await act(async () => {
      screen.getByRole("button", { name: "重试" }).click();
    });
    expect(await screen.findByRole("button", { name: "刷新应用" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
    expect(mocks.loadFeature).toHaveBeenCalledTimes(2);
    errorSpy.mockRestore();
  });

  it("retries a Projects feature without resetting its loaded Skills runtime", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.activeOverlay = "projects";
    let retryReady = false;
    mocks.loadFeature.mockImplementation(() => retryReady
      ? Promise.resolve(panel("Projects panel"))
      : Promise.reject(new Error("Failed to fetch dynamically imported module")));
    mocks.retryFeature.mockImplementation(() => {
      retryReady = true;
      return true;
    });
    render(<WorkspaceOverlayHost />);

    await screen.findByRole("button", { name: "重试" });
    await act(async () => {
      screen.getByRole("button", { name: "重试" }).click();
    });
    expect(await screen.findByText("Projects panel")).toBeTruthy();
    expect(mocks.retryFeature).toHaveBeenCalledWith("projects");
    expect(mocks.retrySkillsRuntime).not.toHaveBeenCalled();
    expect(mocks.loadSkillsRuntime).toHaveBeenCalledTimes(1);
    errorSpy.mockRestore();
  });

  it("retries the Skills runtime without consuming the Projects feature retry", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.activeOverlay = "projects";
    let retryReady = false;
    mocks.loadSkillsRuntime.mockImplementation(() => retryReady
      ? Promise.resolve({
        default: ({ children }: PropsWithChildren) => children,
      } satisfies { default: ComponentType<PropsWithChildren> })
      : Promise.reject(new Error("Failed to fetch dynamically imported module")));
    mocks.retrySkillsRuntime.mockImplementation(() => {
      retryReady = true;
      return true;
    });
    mocks.loadFeature.mockResolvedValue(panel("Projects panel"));
    render(<WorkspaceOverlayHost />);

    await screen.findByRole("button", { name: "重试" });
    await act(async () => {
      screen.getByRole("button", { name: "重试" }).click();
    });
    expect(await screen.findByText("Projects panel")).toBeTruthy();
    expect(mocks.retrySkillsRuntime).toHaveBeenCalledTimes(1);
    expect(mocks.retryFeature).not.toHaveBeenCalled();
    expect(mocks.loadFeature).toHaveBeenCalledTimes(1);
    errorSpy.mockRestore();
  });
});
