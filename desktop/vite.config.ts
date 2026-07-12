import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

interface ModuleInfoReader {
  (id: string): {
    importers: readonly string[];
    dynamicImporters: readonly string[];
    isEntry?: boolean;
  } | null;
}

function reachesStaticEntry(
  id: string,
  getModuleInfo: ModuleInfoReader,
  visiting = new Set<string>(),
): boolean {
  if (visiting.has(id)) return false;
  const info = getModuleInfo(id);
  if (!info) return false;
  if (info.isEntry) return true;
  const next = new Set(visiting);
  next.add(id);
  return info.importers.some((importer) => reachesStaticEntry(
    importer,
    getModuleInfo,
    next,
  ));
}

function belongsOnlyToOfficeContent(
  id: string,
  getModuleInfo: ModuleInfoReader,
  visiting = new Set<string>(),
): boolean {
  const normalized = id.replaceAll("\\", "/");
  if (normalized.endsWith("/src/v1/components/OfficeMarkdown.tsx")) return true;
  if (visiting.has(id)) return true;
  const info = getModuleInfo(id);
  const importers = info ? [...info.importers, ...info.dynamicImporters] : [];
  if (!importers.length) return false;
  const next = new Set(visiting);
  next.add(id);
  return importers.every((importer) => belongsOnlyToOfficeContent(
    importer,
    getModuleInfo,
    next,
  ));
}

function productionChunk(
  id: string,
  context: { getModuleInfo: ModuleInfoReader },
): string | undefined {
  const normalized = id.replaceAll("\\", "/");
  if (normalized.includes("/node_modules/")) {
    if (belongsOnlyToOfficeContent(id, context.getModuleInfo)) {
      return "office-content-runtime";
    }
    if (!reachesStaticEntry(id, context.getModuleInfo)) return undefined;
    return "vendor-runtime";
  }
  if (
    normalized.endsWith("/src/v1/state/artifactPreviewState.ts")
    || normalized.endsWith("/src/v1/state/artifactPreviewFailure.ts")
  ) {
    return undefined;
  }
  if (
    normalized.includes("/src/v1/api/")
    || normalized.includes("/src/v1/state/")
  ) {
    if (!reachesStaticEntry(id, context.getModuleInfo)) return undefined;
    return "ecorex-runtime-client";
  }
  if (
    normalized.endsWith("/src/v1/components/IconButton.tsx")
    || normalized.endsWith("/src/v1/components/TechnicalDetails.tsx")
  ) {
    return "ecorex-ui-primitives";
  }
  return undefined;
}

export default defineConfig({
  base: "./",
  publicDir: false,
  plugins: [react()],
  resolve: {
    alias: {
      // The workspace owns viewport scrolling. Radix's transitive
      // react-remove-scroll dependency otherwise injects a runtime <style>,
      // which a production `style-src 'self'` policy must reject.
      "react-style-singleton": fileURLToPath(new URL(
        "./src/v1/vendor/cspStyleSingleton.ts",
        import.meta.url,
      )),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // Stable architecture layers keep lazy feature chunks downstream of
        // shared code. This is required by the content-addressed release DAG
        // and deliberately avoids per-library or per-feature chunk lists.
        manualChunks: productionChunk,
        // Rollup hashes are only build-graph placeholders. The production
        // post-build gate rewrites every file to its final SHA-256 identity.
        entryFileNames: "assets/[name].unhashed-[hash].js",
        chunkFileNames: "assets/[name].unhashed-[hash].js",
        assetFileNames: "assets/[name].unhashed-[hash][extname]"
      }
    }
  }
});
