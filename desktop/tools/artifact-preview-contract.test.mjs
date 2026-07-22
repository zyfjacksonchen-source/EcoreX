import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const preview = await readFile(
  new URL("../src/v1/components/ArtifactPreviewDialog.tsx", import.meta.url),
  "utf-8",
);
const shelf = await readFile(
  new URL("../src/v1/components/ArtifactShelf.tsx", import.meta.url),
  "utf-8",
);
const features = await readFile(
  new URL("../src/v1/styles/features.css", import.meta.url),
  "utf-8",
);
const runtimeSession = await readFile(
  new URL("../src/v1/state/useRuntimeSession.ts", import.meta.url),
  "utf-8",
);
const previewCache = await readFile(
  new URL("../src/v1/state/artifactPreviewCache.ts", import.meta.url),
  "utf-8",
);
const previewState = await readFile(
  new URL("../src/v1/state/artifactPreviewState.ts", import.meta.url),
  "utf-8",
);
const viteConfig = await readFile(
  new URL("../vite.config.ts", import.meta.url),
  "utf-8",
);
const cspStyleSingleton = await readFile(
  new URL("../src/v1/vendor/cspStyleSingleton.ts", import.meta.url),
  "utf-8",
);

test("clicking the Artifact card opens preview without a separate magnifier affordance", () => {
  assert.match(shelf, /className="ex-artifact-primary"[\s\S]*onClick=\{\(\) => onAction\?\.\(artifact, "preview"\)\}/u);
  assert.doesNotMatch(shelf, /label="(?:放大|缩小|打开)预览"/u);
});

test("image preview opens fitted and keeps explicit bounded zoom controls", () => {
  assert.match(preview, /const IMAGE_ZOOM_STEPS = \[1, 1\.25, 1\.5, 2, 3, 4\] as const/u);
  assert.match(preview, /const \[zoomIndex, setZoomIndex\] = useState\(0\)/u);
  assert.match(preview, /setZoomIndex\(0\)/u);
  assert.match(preview, /Math\.min\(IMAGE_ZOOM_STEPS\.length - 1,/u);
  assert.match(preview, /Math\.max\(0,/u);
  assert.match(preview, /zoom === 1 \? "适合窗口"/u);
  assert.match(preview, /label="显示完整图片" disabled=\{zoom === 1\}/u);
  assert.match(preview, /className=\{`ex-preview-media-canvas is-zoom-\$\{Math\.round\(zoom \* 100\)\}`\}/u);
  assert.doesNotMatch(preview, /style=\{\{/u);
});

test("Radix modal scroll locking cannot inject runtime styles under the strict CSP", () => {
  assert.match(viteConfig, /"react-style-singleton": fileURLToPath/u);
  assert.match(viteConfig, /cspStyleSingleton\.ts/u);
  assert.match(cspStyleSingleton, /export function styleSingleton/u);
  assert.match(cspStyleSingleton, /return null/u);
  assert.doesNotMatch(cspStyleSingleton, /createElement\(["']style["']\)/u);
  assert.doesNotMatch(cspStyleSingleton, /document\.head/u);
});

test("the fitted canvas uses the complete viewport and never crops the image", () => {
  assert.match(features, /\.ex-preview-dialog\s*\{[\s\S]*height:\s*calc\(100dvh - var\(--space-6\)\)/u);
  assert.match(features, /\.ex-preview-body\s*\{[\s\S]*overflow:\s*auto/u);
  assert.match(features, /\.ex-preview-media-canvas\s*\{[\s\S]*min-width:\s*100%[\s\S]*min-height:\s*100%/u);
  const imageRule = features.match(/\.ex-preview-media-canvas img\s*\{([^}]*)\}/u)?.[1] ?? "";
  assert.match(imageRule, /width:\s*100%/u);
  assert.match(imageRule, /height:\s*100%/u);
  assert.match(imageRule, /object-fit:\s*contain/u);
  assert.doesNotMatch(imageRule, /object-fit:\s*cover/u);
});

test("media thumbnails load near the viewport through a bounded abortable LRU", () => {
  assert.match(shelf, /new IntersectionObserver/u);
  assert.match(shelf, /rootMargin:\s*"240px 0px"/u);
  assert.match(shelf, /data-preview-artifact-id/u);
  assert.match(runtimeSession, /prefetchArtifactPreview/u);
  assert.match(runtimeSession, /fetchPreview:[\s\S]*artifactBlob\([\s\S]*"thumbnail"/u);
  assert.match(runtimeSession, /loadArtifactPreview[\s\S]*artifactBlob\(artifact\.artifact_id, "preview"/u);
  assert.doesNotMatch(
    runtimeSession,
    /for \(const artifact of effectiveArtifacts\)[\s\S]*artifactBlob/u,
  );
  assert.match(previewCache, /DEFAULT_MAX_ENTRIES = 24/u);
  assert.match(previewCache, /ARTIFACT_PREVIEW_MAX_BYTES = 64 \* 1024 \* 1024/u);
  assert.match(previewCache, /DEFAULT_MAX_CONCURRENT = 4/u);
  assert.match(previewCache, /request\.controller\.abort\(\)/u);
  assert.match(previewCache, /revokeObjectUrl/u);
});

test("a late preview response cannot overwrite a newer or closed dialog", () => {
  assert.match(preview, /const requestSequence = useRef\(0\)/u);
  assert.match(preview, /const requestId = requestSequence\.current \+ 1/u);
  assert.match(preview, /const controller = new AbortController\(\)/u);
  assert.match(preview, /controller\.signal\.aborted \|\| requestSequence\.current !== requestId/u);
  assert.match(preview, /settleArtifactPreview\(current, requestId, \{\s*status: "ready"/u);
  assert.match(previewState, /if \(!current \|\| current\.request_id !== requestId\) return current;/u);
  assert.match(preview, /requestSequence\.current \+= 1;\s*requestController\.current\?\.abort\(\);/u);
  assert.match(preview, /const url = URL\.createObjectURL\(blob\)/u);
  assert.match(preview, /URL\.revokeObjectURL\(url\);/u);
});

test("preview failures are visible, retryable in place, and corrupt images leave ready state", () => {
  assert.match(previewState, /status: "loading"/u);
  assert.match(previewState, /status: "ready"/u);
  assert.match(previewState, /status: "error"/u);
  assert.match(preview, /currentPreview\?\.status === "error"/u);
  assert.match(preview, /role="alert"/u);
  assert.match(preview, /onClick=\{requestPreview\}/u);
  assert.match(preview, /image\.decode\(\)\.catch/u);
  assert.match(preview, /failArtifactPreviewDecode\(current, url\)/u);
  assert.match(preview, /onError=\{\(\) => \{/u);
  assert.match(preview, /const ownedUrl = useRef<string \| null>\(null\)/u);
  assert.match(preview, /URL\.revokeObjectURL\(ownedUrl\.current\);/u);
  assert.match(preview, /ownedUrl\.current = null;/u);
  assert.match(preview, /requestController\.current\?\.abort\(\);[\s\S]*releaseOwnedUrl\(\);/u);
});

test("the dialog exposes only backend-authorized artifact actions", () => {
  assert.match(preview, /artifact\.actions\.includes\("preview"\)/u);
  assert.match(preview, /artifact\?\.actions\.includes\("download"\)/u);
});
