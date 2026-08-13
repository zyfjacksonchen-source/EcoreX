import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const timeline = await readFile(
  new URL("../src/v1/components/Timeline.tsx", import.meta.url),
  "utf-8",
);
const turnProjection = await readFile(
  new URL("../src/v1/state/timelineTurns.ts", import.meta.url),
  "utf-8",
);
const runtimeSession = await readFile(
  new URL("../src/v1/state/useRuntimeSession.ts", import.meta.url),
  "utf-8",
);
const features = await readFile(
  new URL("../src/v1/styles/features.css", import.meta.url),
  "utf-8",
);
const richMessage = await readFile(
  new URL("../src/v1/components/OfficeMarkdown.tsx", import.meta.url),
  "utf-8",
);
const activity = await readFile(
  new URL("../src/v1/components/TimelineActivity.tsx", import.meta.url),
  "utf-8",
);
const sidebar = await readFile(
  new URL("../src/v1/components/Sidebar.tsx", import.meta.url),
  "utf-8",
);
const attachmentPreview = await readFile(
  new URL("../src/v1/components/InputAttachmentPreview.tsx", import.meta.url),
  "utf-8",
);
const composer = await readFile(
  new URL("../src/v1/components/Composer.tsx", import.meta.url),
  "utf-8",
);
const artifactShelf = await readFile(
  new URL("../src/v1/components/ArtifactShelf.tsx", import.meta.url),
  "utf-8",
);
const imageGallery = await readFile(
  new URL("../src/v1/state/timelineImageGallery.ts", import.meta.url),
  "utf-8",
);

test("the chat DOM virtualizes durable turn projections", () => {
  assert.match(timeline, /from "react-virtuoso"/u);
  assert.match(timeline, /items\.filter\(\(item\) => item\.kind !== "task_list"\)/u);
  assert.match(timeline, /buildTimelineTurns\(turns, timelineItems, interactions\)/u);
  assert.match(timeline, /computeItemKey=\{\(_index, entry\) => entry\.turn\.turn_id\}/u);
  assert.match(timeline, /increaseViewportBy=\{\{ top: 800, bottom: 800 \}\}/u);
  assert.match(timeline, /atBottomThreshold=\{TIMELINE_BOTTOM_THRESHOLD_PX\}/u);
  assert.match(timeline, /TIMELINE_BOTTOM_THRESHOLD_PX = 72/u);
  assert.match(turnProjection, /function sequence\(block: TimelineBlock\)/u);
  assert.match(turnProjection, /return leftSeq - rightSeq/u);
  assert.match(turnProjection, /\.filter\(isPresent\)/u);
  assert.match(turnProjection, /created_seq/u);
  assert.doesNotMatch(timeline, /selectTimelineWindow|historyEndAnchorId/u);
  assert.match(timeline, /item\.kind === "artifact"/u);
  assert.match(timeline, /selectUnbackedArtifactProjections\(itemArtifacts, visibleArtifacts\)/u);
  assert.doesNotMatch(timeline, /item\.content\.arguments/u);
  assert.match(timeline, /lazy\(\(\) => import\("\.\/TimelineActivity\.tsx"\)\)/u);
  assert.doesNotMatch(activity, /content\.(?:arguments|result|path)/u);
  assert.match(activity, /activity\.display_label/u);
  assert.match(activity, /工作步骤/u);
  assert.doesNotMatch(activity, /TOOL_LABELS|function toolLabel/u);
});

test("streaming deltas batch by frame but terminal facts flush synchronously", () => {
  assert.match(runtimeSession, /event_type === "item\.delta" \|\| event\.event_type === "reasoning\.delta"/u);
  assert.match(runtimeSession, /window\.requestAnimationFrame\(flushEvents\)/u);
  assert.match(runtimeSession, /window\.setTimeout\(flushEvents, 50\)/u);
  assert.match(runtimeSession, /!isFrameBatchableEvent\(event\) \|\| pendingEvents\.length >= 128/u);
});

test("completed rows use native rendering containment while the active row stays live", () => {
  assert.match(timeline, /item\.status === "in_progress"/u);
  assert.match(features, /\.ex-message:not\(\.is-streaming\)\s*\{[\s\S]*content-visibility:\s*auto/u);
  assert.match(features, /contain-intrinsic-size:\s*auto 84px/u);
});

test("uploaded images keep authenticated thumbnails and open a fit-to-screen preview", () => {
  assert.match(timeline, /<InputAttachmentPreview/u);
  assert.match(attachmentPreview, /loadThumbnailBlob\(attachment\.attachment_id, controller\.signal\)/u);
  assert.match(attachmentPreview, /if \(!isImage \|\| !dialogOpen\) return;[\s\S]*loadBlob\(attachment\.attachment_id, controller\.signal\)/u);
  assert.match(attachmentPreview, /<Dialog\.Root open=\{dialogOpen\} onOpenChange=\{setDialogOpen\}>/u);
  assert.match(attachmentPreview, /URL\.createObjectURL\(blob\)/u);
  assert.match(attachmentPreview, /<Dialog\.Content/u);
  assert.match(features, /\.ex-attachment-preview-dialog > img\s*\{[\s\S]*object-fit:\s*contain/u);
  assert.match(features, /\.ex-input-attachment-preview-trigger img[\s\S]*object-fit:\s*cover/u);
});

test("the composer exposes uploading and ready attachment states", () => {
  assert.match(composer, /pendingAttachments/u);
  assert.match(composer, /URL\.createObjectURL\(file\)/u);
  assert.match(composer, />正在上传</u);
  assert.match(attachmentPreview, /"已就绪"/u);
  assert.match(composer, /<InputAttachmentPreview/u);
});

test("jump to latest is a transient centered circular affordance", () => {
  assert.match(timeline, /showJumpToLatest \? \(/u);
  assert.match(timeline, /aria-label="回到底部"/u);
  assert.doesNotMatch(timeline, /回到底部\s*<\/button>/u);
  assert.match(features, /\.ex-timeline-jump\s*\{[\s\S]*left:\s*50%[\s\S]*transform:\s*translateX\(-50%\)/u);
  assert.match(features, /\.ex-timeline-jump-button\s*\{[\s\S]*width:\s*32px[\s\S]*border-radius:\s*var\(--radius-pill\)/u);
});

test("timeline relayout remeasures the real bottom without stealing an upward scroll", () => {
  assert.match(
    timeline,
    /totalListHeightChanged=\{\(\) => \{[\s\S]{0,300}syncFollowStateRef\.current\(true\)/u,
  );
  assert.match(
    timeline,
    /followPausedByUserRef\.current\s*&& atBottom\s*&& \(resumeAtBottomRef\.current \|\| layoutChanged\)/u,
  );
  assert.match(
    timeline,
    /else if \(followPausedByUserRef\.current\) \{[\s\S]{0,200}setShowJumpToLatest\(true\)/u,
  );
});

test("the conversation directory reuses virtualized turn identity and scrolling", () => {
  assert.match(timeline, /aria-label="对话目录"/u);
  assert.match(timeline, /timelineTurns\.map\(\(entry, index\) =>/u);
  assert.match(timeline, /key=\{entry\.turn\.turn_id\}/u);
  assert.match(timeline, /directorySummary\(entry\.turn\.input, index\)/u);
  assert.match(
    timeline,
    /virtuosoRef\.current\?\.scrollToIndex\(\{ index, align: "start", behavior: "auto" \}\)/u,
  );
  assert.match(timeline, /directoryJumpTargetRef/u);
  assert.match(timeline, /if \(directoryJumpTargetRef\.current !== null\) return/u);
  assert.match(timeline, /aria-current=\{directoryIndex === index \? "location" : undefined\}/u);
  assert.match(timeline, /rangeChanged=\{\(\{ startIndex, endIndex \}\) =>/u);
  assert.match(timeline, /setDirectoryIndex\(startIndex\)/u);
  assert.doesNotMatch(timeline, /setDirectoryIndex\(endIndex\)/u);
  assert.match(timeline, /itemBottom - list\.clientHeight/u);
  assert.match(timeline, /<Minus aria-hidden="true" strokeLinecap="butt" \/>/u);
  assert.match(timeline, /<Tooltip\.Content className="ex-tooltip ex-timeline-directory-tooltip"/u);
  assert.match(features, /\.ex-timeline-directory-list\s*\{[\s\S]*max-height:\s*calc\(100dvh - 176px\);[\s\S]*mask-image:\s*linear-gradient\(to bottom, rgb\(0 0 0 \/ 25%\), #000 48px\)/u);
  assert.match(features, /\.ex-timeline-directory-item\s*\{[\s\S]*width:\s*24px;[\s\S]*height:\s*20px;[\s\S]*min-height:\s*20px;/u);
  assert.match(features, /\.ex-timeline-directory-item svg\s*\{[\s\S]*width:\s*21px;[\s\S]*height:\s*20px;[\s\S]*stroke-width:\s*4px;[\s\S]*opacity:\s*0\.18;/u);
  assert.match(features, /\.ex-timeline-directory-item\[aria-current="location"\] svg\s*\{[\s\S]*opacity:\s*0\.55;/u);
});

test("streaming output has one automatic scroll owner", () => {
  assert.doesNotMatch(timeline, /followOutput=/u);
  assert.doesNotMatch(
    timeline,
    /useEffect\(\(\) => \{[\s\S]{0,700}scrollToIndex\([\s\S]{0,300}\[contentRevision/u,
  );
  assert.doesNotMatch(
    timeline,
    /else if \(followLatestRef\.current\) \{[\s\S]{0,160}scrollParent\.scrollTop/u,
  );
  assert.match(
    timeline,
    /totalListHeightChanged=\{\(\) => \{[\s\S]{0,300}else if \(followLatestRef\.current\) scrollParent\.scrollTop/u,
  );
});

test("switching conversations gives Virtuoso a fresh measurement owner", () => {
  assert.match(timeline, /<Virtuoso[\s\S]{0,180}key=\{timelineThreadId\}/u);
});

test("assistant office Markdown is lazy, bounded, and cannot load raw HTML or images", () => {
  assert.match(timeline, /lazy\(\(\) => import\("\.\/OfficeMarkdown\.tsx"\)\)/u);
  assert.match(timeline, /<Suspense fallback=/u);
  assert.match(richMessage, /skipHtml/u);
  assert.match(richMessage, /remarkGfm/u);
  assert.match(richMessage, /MARKDOWN_PARSE_LIMIT = 256 \* 1024/u);
  assert.match(richMessage, /SAFE_PROTOCOLS = new Set\(\["http:", "https:", "mailto:"\]\)/u);
  assert.match(richMessage, /img: \(\{ alt \}\) =>/u);
  assert.doesNotMatch(richMessage, /dangerouslySetInnerHTML/u);
});

test("streaming Markdown renders the frame-batched text without a second delay", () => {
  assert.doesNotMatch(richMessage, /STREAM_FLUSH_MS/u);
  assert.doesNotMatch(richMessage, /window\.setTimeout/u);
  assert.doesNotMatch(richMessage, /useDeferredValue/u);
  assert.match(richMessage, /\{text\}/u);
});

test("multiple independent image results use accessible gallery controls", () => {
  assert.match(timeline, /groupTimelineImageArtifacts\(entry\.blocks\)/u);
  assert.match(imageGallery, /artifact\?\.family === "image" && artifact\.status === "ready"/u);
  assert.match(imageGallery, /typeof block\.item\.content\.retouch_job_id === "string"/u);
  assert.match(imageGallery, /if \(images\.length < 2\) return \[\.\.\.blocks\]/u);
  assert.match(imageGallery, /slots: images\.map\(\(block\) => \(\{ kind: "artifact", block \}\)\)/u);
  assert.doesNotMatch(imageGallery, /image_batch|batchFailures|schema_version/u);
  assert.match(artifactShelf, /"ArrowLeft"/u);
  assert.match(artifactShelf, /"ArrowRight"/u);
  assert.match(artifactShelf, /data-artifact-status/u);
  assert.match(artifactShelf, /ex-image-generation-canvas/u);
  assert.match(artifactShelf, /aria-live="polite"/u);
  assert.match(features, /\.ex-image-gallery-track[\s\S]*scroll-snap-type:\s*inline mandatory/u);
  assert.match(features, /prefers-reduced-motion[\s\S]*\.ex-image-gallery-track/u);
});

test("failed image calls do not synthesize gallery placeholders", () => {
  assert.doesNotMatch(runtimeSession, /imageBatchFacts|artifact\.image\.batch_/u);
  assert.doesNotMatch(timeline, /imageBatchFailures/u);
  assert.doesNotMatch(artifactShelf, /data-image-batch-task-id/u);
});

test("continuing by task ID keeps the current transcript until the target is verified", () => {
  assert.match(sidebar, /按任务 ID 继续/u);
  assert.match(sidebar, /复制任务 ID/u);
  assert.match(sidebar, /读取并继续/u);
  const switchStart = runtimeSession.indexOf("const openThread = useCallback");
  const switchEnd = runtimeSession.indexOf("const pendingThreadRequestId", switchStart);
  const switchContract = runtimeSession.slice(switchStart, switchEnd);
  const projectionRead = switchContract.indexOf("await client.projection(targetThreadId");
  const selectionCommit = switchContract.indexOf("selectedThreadId.current = targetThreadId");
  const artifactCommit = switchContract.indexOf("clearArtifactView()");
  assert.ok(projectionRead >= 0, "task projection must be verified");
  assert.ok(selectionCommit > projectionRead, "selection changes only after projection verification");
  assert.ok(artifactCommit > projectionRead, "the visible artifact view is retained while verifying");
  assert.doesNotMatch(switchContract, /clearThreadProjection\(\)/u);
});
