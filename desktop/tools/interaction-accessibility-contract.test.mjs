import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const tokens = await readFile(
  new URL("../src/styles/tokens.css", import.meta.url),
  "utf-8",
);
const primitives = await readFile(
  new URL("../src/v1/styles/primitives.css", import.meta.url),
  "utf-8",
);
const features = await readFile(
  new URL("../src/v1/styles/features.css", import.meta.url),
  "utf-8",
);
const artifactShelf = await readFile(
  new URL("../src/v1/components/ArtifactShelf.tsx", import.meta.url),
  "utf-8",
);
const app = await readFile(
  new URL("../src/v1/AppV1.tsx", import.meta.url),
  "utf-8",
);
const interactionStack = await readFile(
  new URL("../src/v1/components/InteractionStack.tsx", import.meta.url),
  "utf-8",
);
const composer = await readFile(
  new URL("../src/v1/components/Composer.tsx", import.meta.url),
  "utf-8",
);
const runtimeClient = await readFile(
  new URL("../src/v1/api/runtimeClient.ts", import.meta.url),
  "utf-8",
);

test("forced colors retain system contrast and explicit focus", () => {
  const forced = tokens.match(/@media \(forced-colors: active\) \{([\s\S]*?)\n\}/u)?.[1] ?? "";
  assert.match(forced, /--color-canvas:\s*Canvas/u);
  assert.match(forced, /--color-ink:\s*CanvasText/u);
  assert.match(forced, /--color-accent:\s*Highlight/u);
  assert.match(forced, /--color-accent-ink:\s*HighlightText/u);
  assert.match(forced, /--color-rule:\s*ButtonBorder/u);
  assert.match(primitives, /@media \(forced-colors: active\)[\s\S]*:focus-visible[\s\S]*outline:\s*2px solid Highlight/u);
  assert.match(primitives, /forced-color-adjust:\s*none/u);
});

test("reduced motion removes component animation and zeroes motion tokens", () => {
  const reduced = tokens.match(/@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\n\}/u)?.[1] ?? "";
  assert.match(reduced, /--duration-fast:\s*0ms/u);
  assert.match(reduced, /--duration-base:\s*0ms/u);
  assert.match(reduced, /--duration-slow:\s*0ms/u);
  assert.match(primitives, /@media \(prefers-reduced-motion: reduce\)[\s\S]*animation:\s*none/u);
  assert.match(features, /@media \(prefers-reduced-motion: reduce\)[\s\S]*transition:\s*none/u);
});

test("coarse pointers and narrow artifact containers receive 44px touch actions", () => {
  assert.match(tokens, /--target-touch:\s*44px/u);
  assert.match(artifactShelf, /return props\.asSheet \? <ActionSheet \{\.\.\.props\} \/> : <MoreMenu/u);
  assert.match(artifactShelf, /asSheet=\{coarsePointer\}/u);
  assert.match(features, /@media \(pointer: coarse\)[\s\S]*\.ex-artifact-actions[\s\S]*opacity:\s*1/u);
  assert.match(
    features,
    /@container workspace \(max-width: 680px\)[\s\S]*\.ex-artifact-more[\s\S]*width:\s*var\(--target-touch\)[\s\S]*height:\s*var\(--target-touch\)/u,
  );
  assert.match(features, /\.ex-artifact-actions > :not\(\.ex-artifact-more\)[\s\S]*display:\s*none/u);
});

test("clipboard denial exposes the task id and never reports success", () => {
  assert.match(app, /await navigator\.clipboard\.writeText\(currentThreadId\)/u);
  assert.match(app, /setArtifactNotice\("任务 ID 已复制。"\)/u);
  assert.match(app, /catch \{\s*setArtifactNotice\(`未能自动复制。任务 ID：\$\{currentThreadId\}`\);/u);
});

test("typed HITL renders labelled fields, live errors, and backend actions", () => {
  assert.match(interactionStack, /interaction\.contract\.fields\.map/u);
  assert.match(interactionStack, /htmlFor=\{inputId\}/u);
  assert.match(interactionStack, /role="alert"/u);
  assert.match(interactionStack, /aria-busy=\{busy\}/u);
  assert.match(interactionStack, /action_id:\s*action\.action_id/u);
  assert.match(interactionStack, /data-style=\{action\.style\}/u);
  assert.match(features, /\.ex-interaction-action[\s\S]*border-color:\s*transparent/u);
});

test("HITL retry retains one client request identity until acknowledgement", () => {
  assert.match(interactionStack, /pendingResponses/u);
  assert.match(interactionStack, /existing \?\? \{[\s\S]*crypto\.randomUUID/u);
  assert.match(interactionStack, /pending\.clientRequestId/u);
  assert.match(
    interactionStack,
    /pendingResponses\.current\.delete\(interaction\.interaction_id\)/u,
  );
  assert.match(runtimeClient, /client_request_id:\s*clientRequestId/u);
  assert.doesNotMatch(
    runtimeClient.match(/respondInteraction\([\s\S]*?\n  \}/u)?.[0] ?? "",
    /createClientRequestId\("interaction"\)/u,
  );
});

test("connector login uses dedicated lifecycle routes and keeps server authority", () => {
  const connectorBranch = interactionStack.indexOf(
    'if (interaction.kind === "connector_login")',
  );
  const ordinaryValidation = interactionStack.indexOf("const validationError = validateDraft");
  assert.ok(connectorBranch >= 0);
  assert.ok(ordinaryValidation > connectorBranch);
  assert.match(interactionStack, /connectorLoginInteraction/u);
  assert.match(interactionStack, /refreshProjection/u);
  assert.match(
    interactionStack,
    /action\.action_type === "cancel"[\s\S]*cancelConnectorLogin\(interaction\)/u,
  );
  assert.match(runtimeClient, /operation:\s*"begin"/u);
  assert.match(runtimeClient, /operation:\s*"check"/u);
  assert.match(runtimeClient, /operation:\s*"cancel"/u);
  assert.match(runtimeClient, /connector-login\/\$\{operation\}/u);
  assert.doesNotMatch(
    runtimeClient.match(/connectorLoginInteraction\([\s\S]*?\n  \}/u)?.[0] ?? "",
    /\/respond/u,
  );
});

test("the composer follows CowAgent active-turn send, stop, and steer semantics", () => {
  assert.match(composer, /const primaryActionLabel = active \? "停止当前任务" : sendLabel/u);
  assert.match(composer, /data-mode=\{active \? "stop" : "send"\}/u);
  assert.match(composer, /onClick=\{\(\) => active \? onInterrupt\(\) : void submit\(\)\}/u);
  assert.match(composer, /<IconButton[\s\S]*label="追加到当前任务"[\s\S]*onClick=\{\(\) => void submit\(\)\}/u);
  assert.match(composer, /const sent = await onSend\(\s*draft,\s*"steer",/u);
  assert.match(composer, /event\.key === "Enter"[\s\S]*void submit\(\)/u);
  assert.doesNotMatch(composer, /dispositionLabel|setDisposition|ex-disposition/u);
  assert.doesNotMatch(composer, /"queue"|"replace"/u);
});
