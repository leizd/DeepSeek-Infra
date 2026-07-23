import { createHash } from "node:crypto";
import { existsSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

interface ViteChunkMetadata {
  importedCss?: Set<string>;
}

function workspaceAssetManifest(): Plugin {
  return {
    name: "workspace-asset-manifest",
    writeBundle(options, bundle) {
      if (!options.dir) throw new Error("workspace asset manifest requires an output directory");
      const core = new Set<string>();

      const collectChunk = (fileName: string) => {
        if (core.has(fileName)) return;
        const output = bundle[fileName];
        if (!output || output.type !== "chunk") return;
        core.add(fileName);
        output.imports.forEach(collectChunk);
        const metadata = output.viteMetadata as ViteChunkMetadata | undefined;
        metadata?.importedCss?.forEach((css) => core.add(css));
      };

      Object.values(bundle)
        .filter((output) => output.type === "chunk" && output.isEntry)
        .forEach((output) => collectChunk(output.fileName));

      const runtimeAssets = Object.entries(bundle)
        .filter(([fileName, output]) =>
          existsSync(resolve(options.dir!, fileName)) && (
            fileName.endsWith(".css") ||
            (fileName.endsWith(".js") && output.type === "chunk" && output.code.trim().length > 0)
          ),
        )
        .map(([fileName]) => fileName)
        .sort();
      const buildId = createHash("sha256").update(runtimeAssets.join("\n")).digest("hex").slice(0, 16);
      const withUiBase = (fileName: string) => `/ui/${fileName}`;
      writeFileSync(
        resolve(options.dir, "workspace-assets.json"),
        `${JSON.stringify({
          buildId,
          core: runtimeAssets.filter((fileName) => core.has(fileName)).map(withUiBase),
          optional: runtimeAssets.filter((fileName) => !core.has(fileName)).map(withUiBase),
        }, null, 2)}\n`,
        "utf8",
      );
    },
  };
}

export default defineConfig({
  plugins: [react(), workspaceAssetManifest()],
  base: "/ui/",
  build: {
    outDir: fileURLToPath(new URL("../static/ui", import.meta.url)),
    emptyOutDir: true,
    manifest: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
      "/readyz": "http://127.0.0.1:8000",
    },
  },
});
