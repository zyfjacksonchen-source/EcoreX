import assert from "node:assert/strict";
import test from "node:test";

import type { ArtifactProjection } from "../api/contracts.ts";
import {
  artifactRevisionIdentity,
  selectUnbackedArtifactProjections,
} from "./timelineArtifacts.ts";

function artifact(artifactId: string, revisionId: string): ArtifactProjection {
  return {
    artifact_id: artifactId,
    revision_id: revisionId,
    family: "image",
    role: "deliverable",
    visibility: "primary",
    status: "ready",
    display_name: `${revisionId}.png`,
    mime_type: "image/png",
    size_bytes: 128,
    sha256: "a".repeat(64),
    created_at: "2026-07-22T00:00:00Z",
    lineage: { source_artifact_ids: [], supersedes_revision_id: null },
    renditions: [],
    actions: ["preview"],
    feedback: null,
    quality_evidence: {
      status: "passed",
      checks: [],
      score: 1,
      summary: null,
    },
  };
}

test("exact Artifact Item revisions are not duplicated by the recovery shelf", () => {
  const eventArtifact = artifact("artifact-one", "revision-one");
  assert.deepEqual(
    selectUnbackedArtifactProjections([eventArtifact], [eventArtifact]),
    [],
  );
});

test("a newer projection without an Artifact Item remains visible as a recovery fallback", () => {
  const oldRevision = artifact("artifact-one", "revision-one");
  const recoveredRevision = artifact("artifact-one", "revision-two");
  assert.deepEqual(
    selectUnbackedArtifactProjections([oldRevision], [recoveredRevision]),
    [recoveredRevision],
  );
  assert.notEqual(
    artifactRevisionIdentity(oldRevision),
    artifactRevisionIdentity(recoveredRevision),
  );
});
