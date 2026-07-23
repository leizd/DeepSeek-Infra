import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  createFrontendBuildIdentity,
  digestFrontendSources,
  resolveFrontendSourceRevision,
} from "../../buildIdentity";

const temporaryDirectories: string[] = [];

function temporaryFrontend(): { repository: string; frontend: string } {
  const repository = mkdtempSync(resolve(tmpdir(), "deepseek-build-identity-"));
  temporaryDirectories.push(repository);
  const frontend = resolve(repository, "frontend");
  mkdirSync(resolve(frontend, "public"), { recursive: true });
  writeFileSync(resolve(frontend, "index.html"), "<main>one</main>\n", "utf8");
  writeFileSync(resolve(frontend, "public", "sw.js"), "const worker = 'one';\n", "utf8");
  return { repository, frontend };
}

function git(repository: string, ...args: string[]): string {
  return execFileSync("git", args, { cwd: repository, encoding: "utf8" }).trim();
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe("frontend build identity", () => {
  it("is stable for one source revision and configuration", () => {
    const first = createFrontendBuildIdentity({
      version: "4.3.2",
      sourceRevision: "revision-a",
    });
    const second = createFrontendBuildIdentity({
      version: "4.3.2",
      sourceRevision: "revision-a",
    });
    expect(first).toEqual(second);
    expect(first.buildId).toMatch(/^[0-9a-f]{16}$/);
  });

  it("changes the dirty source digest for index and worker-only edits", () => {
    const { frontend } = temporaryFrontend();
    const initial = digestFrontendSources(frontend);
    writeFileSync(resolve(frontend, "index.html"), "<main>two</main>\n", "utf8");
    const indexChanged = digestFrontendSources(frontend);
    writeFileSync(resolve(frontend, "public", "sw.js"), "const worker = 'two';\n", "utf8");
    const workerChanged = digestFrontendSources(frontend);
    expect(indexChanged).not.toBe(initial);
    expect(workerChanged).not.toBe(indexChanged);
  });

  it("uses GITHUB_SHA for formal builds and marks local dirty builds", () => {
    const { repository, frontend } = temporaryFrontend();
    git(repository, "init");
    git(repository, "config", "user.email", "build@example.test");
    git(repository, "config", "user.name", "Build Test");
    git(repository, "add", "frontend");
    git(repository, "commit", "-m", "initial");
    const head = git(repository, "rev-parse", "HEAD");
    expect(resolveFrontendSourceRevision(repository, frontend, {})).toBe(head);
    expect(resolveFrontendSourceRevision(repository, frontend, { GITHUB_SHA: "formal-revision" })).toBe("formal-revision");

    writeFileSync(resolve(frontend, "index.html"), "<main>dirty</main>\n", "utf8");
    const dirty = resolveFrontendSourceRevision(repository, frontend, {});
    expect(dirty).toMatch(new RegExp(`^${head}-dirty-[0-9a-f]{16}$`));
    expect(createFrontendBuildIdentity({ version: "4.3.2", sourceRevision: dirty }).buildId)
      .not.toBe(createFrontendBuildIdentity({ version: "4.3.2", sourceRevision: head }).buildId);
  });
});
