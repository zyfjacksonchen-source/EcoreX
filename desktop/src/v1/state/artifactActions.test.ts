import assert from "node:assert/strict";
import test from "node:test";

import type { ArtifactProjection } from "../api/contracts.ts";
import {
  artifactPrimaryAction,
  artifactUiActions,
  mergeArtifactProjections,
} from "./artifactActions.ts";

test("desktop overflow maps only backend-projected file actions", () => {
  assert.deepEqual(
    artifactUiActions(
      ["preview", "open", "reveal", "download", "feedback", "precise_retouch"],
      false,
    ),
    ["preview", "open", "reveal", "download"],
  );
});

test("compact and touch overflow preserves feedback and precise retouch parity", () => {
  assert.deepEqual(
    artifactUiActions(
      ["preview", "open", "reveal", "download", "feedback", "precise_retouch"],
      true,
    ),
    [
      "thumbs_up",
      "thumbs_down",
      "precise_retouch",
      "preview",
      "open",
      "reveal",
      "download",
    ],
  );
});

test("the WebUI never fabricates actions absent from the backend projection", () => {
  assert.deepEqual(artifactUiActions(["download"], true), ["download"]);
});

test("office files open natively while PDFs and images keep in-app preview", () => {
  const actions = ["preview", "open", "download", "reveal"] as const;
  assert.equal(artifactPrimaryAction("document", actions), "open");
  assert.equal(artifactPrimaryAction("spreadsheet", actions), "open");
  assert.equal(artifactPrimaryAction("presentation", actions), "open");
  assert.equal(artifactPrimaryAction("pdf", actions), "preview");
  assert.equal(artifactPrimaryAction("image", actions), "preview");
});

test("the latest Artifact endpoint projection overrides the replay Item fallback", () => {
  const replay = {
    artifact_id: "artifact-1",
    revision_id: "revision-1",
    created_at: "2026-07-10T10:00:00Z",
    feedback: null,
  } as unknown as ArtifactProjection;
  const current = {
    artifact_id: "artifact-1",
    revision_id: "revision-2",
    created_at: "2026-07-10T10:01:00Z",
    lineage: { supersedes_revision_id: "revision-1", source_artifact_ids: [] },
    feedback: { signal: "thumbs_up" },
  } as unknown as ArtifactProjection;
  assert.deepEqual(mergeArtifactProjections([replay], [current]), [current]);
});

test("a delayed artifact list cannot roll a completed retouch event back", () => {
  const base = {
    artifact_id: "artifact-1",
    revision_id: "revision-1",
    created_at: "2026-07-10T10:00:00Z",
  } as unknown as ArtifactProjection;
  const completed = {
    artifact_id: "artifact-1",
    revision_id: "revision-2",
    created_at: "2026-07-10T10:01:00Z",
    lineage: { supersedes_revision_id: "revision-1", source_artifact_ids: [] },
  } as unknown as ArtifactProjection;
  assert.deepEqual(mergeArtifactProjections([completed], [base]), [completed]);
});
