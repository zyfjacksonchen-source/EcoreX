import assert from "node:assert/strict";
import test from "node:test";

import type { ArtifactProjection } from "../api/contracts.ts";
import { RuntimeApiError } from "../api/runtimeClient.ts";
import { ArtifactPreviewLimitError } from "./artifactPreviewCache.ts";
import { artifactPreviewLoadFailure } from "./artifactPreviewFailure.ts";
import {
  failArtifactPreviewDecode,
  loadingArtifactPreview,
  readyArtifactPreview,
  settleArtifactPreview,
} from "./artifactPreviewState.ts";

const artifact = {
  artifact_id: "artifact-one",
  revision_id: "revision-one",
} as ArtifactProjection;

test("late preview results cannot replace a newer request or reopen a closed preview", () => {
  const newer = loadingArtifactPreview(artifact, 2);
  const lateResult = { status: "ready", url: "blob:late" } as const;
  assert.equal(settleArtifactPreview(newer, 1, lateResult), newer);
  assert.equal(settleArtifactPreview(null, 1, lateResult), null);
});

test("the active request settles into a typed visible error", () => {
  const loading = loadingArtifactPreview(artifact, 3);
  assert.deepEqual(settleArtifactPreview(loading, 3, {
    status: "error",
    code: "not_found",
    message: "预览不存在。",
    can_retry: true,
  }), {
    ...loading,
    status: "error",
    code: "not_found",
    message: "预览不存在。",
    can_retry: true,
  });
});

test("404 and bounded-cache failures become explicit user-facing states", () => {
  assert.deepEqual(
    artifactPreviewLoadFailure(new RuntimeApiError("missing", 404, "preview_missing")),
    {
      status: "error",
      code: "not_found",
      message: "暂时找不到这份预览，它可能仍在生成或已经更新。请重试。",
      can_retry: true,
    },
  );
  assert.deepEqual(artifactPreviewLoadFailure(new ArtifactPreviewLimitError()), {
    status: "error",
    code: "too_large",
    message: "这张预览图过大，无法在页面内安全加载。你仍可保存原文件。",
    can_retry: false,
  });
});

test("a corrupt image decode becomes a retryable error only for the active URL", () => {
  const ready = readyArtifactPreview(artifact, 4, "blob:current");
  assert.equal(failArtifactPreviewDecode(ready, "blob:stale"), ready);
  assert.deepEqual(failArtifactPreviewDecode(ready, "blob:current"), {
    status: "error",
    request_id: 4,
    artifact_id: artifact.artifact_id,
    revision_id: artifact.revision_id,
    code: "decode_failed",
    message: "这张图片无法正确显示，文件可能已损坏。你可以重试加载或保存原文件。",
    can_retry: true,
  });
});

test("retry leaves the error state through a fresh request identity", () => {
  const failed = settleArtifactPreview(loadingArtifactPreview(artifact, 5), 5, {
    status: "error",
    code: "not_found",
    message: "预览不存在。",
    can_retry: true,
  });
  assert.equal(failed?.status, "error");
  assert.deepEqual(loadingArtifactPreview(artifact, 6), {
    status: "loading",
    request_id: 6,
    artifact_id: artifact.artifact_id,
    revision_id: artifact.revision_id,
  });
});
