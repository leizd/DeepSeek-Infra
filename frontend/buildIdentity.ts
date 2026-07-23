import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { relative, resolve } from "node:path";

export const FRONTEND_BUILD_IDENTITY_SCHEMA_VERSION = 1;
export const FRONTEND_BUILD_CONFIGURATION_VERSION = "4.3.2-immutable-build-v1";

export interface FrontendBuildIdentity {
  schemaVersion: 1;
  version: string;
  sourceRevision: string;
  buildId: string;
}

interface BuildIdentityOptions {
  version: string;
  sourceRevision: string;
  buildConfigurationVersion?: string;
}

const EXCLUDED_SOURCE_DIRECTORIES = new Set(["node_modules", "dist", "coverage"]);

function walkFiles(root: string, current = root): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(current, { withFileTypes: true })) {
    if (entry.isDirectory() && EXCLUDED_SOURCE_DIRECTORIES.has(entry.name)) continue;
    const path = resolve(current, entry.name);
    if (entry.isDirectory()) {
      files.push(...walkFiles(root, path));
    } else if (entry.isFile()) {
      files.push(path);
    }
  }
  return files.sort((left, right) => relative(root, left).localeCompare(relative(root, right)));
}

export function digestFrontendSources(frontendRoot: string): string {
  const hash = createHash("sha256");
  for (const path of walkFiles(frontendRoot)) {
    const name = relative(frontendRoot, path).replaceAll("\\", "/");
    hash.update(name);
    hash.update("\0");
    hash.update(readFileSync(path));
    hash.update("\0");
  }
  return hash.digest("hex").slice(0, 16);
}

function gitOutput(repositoryRoot: string, args: string[]): string {
  return execFileSync("git", args, {
    cwd: repositoryRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  }).trim();
}

export function resolveFrontendSourceRevision(
  repositoryRoot: string,
  frontendRoot: string,
  environment: NodeJS.ProcessEnv = process.env,
): string {
  const githubRevision = environment.GITHUB_SHA?.trim();
  if (githubRevision) return githubRevision;

  const sourceDigest = digestFrontendSources(frontendRoot);
  try {
    const head = gitOutput(repositoryRoot, ["rev-parse", "HEAD"]);
    const frontendPath = relative(repositoryRoot, frontendRoot).replaceAll("\\", "/");
    const dirty = gitOutput(repositoryRoot, ["status", "--porcelain", "--untracked-files=all", "--", frontendPath]);
    return dirty ? `${head}-dirty-${sourceDigest}` : head;
  } catch {
    return `local-dirty-${sourceDigest}`;
  }
}

export function createFrontendBuildIdentity({
  version,
  sourceRevision,
  buildConfigurationVersion = FRONTEND_BUILD_CONFIGURATION_VERSION,
}: BuildIdentityOptions): FrontendBuildIdentity {
  const buildId = createHash("sha256")
    .update(`${version}\n${sourceRevision}\n${buildConfigurationVersion}`)
    .digest("hex")
    .slice(0, 16);
  return {
    schemaVersion: FRONTEND_BUILD_IDENTITY_SCHEMA_VERSION,
    version,
    sourceRevision,
    buildId,
  };
}
