import assert from "node:assert/strict";
import test from "node:test";

import type { ArtifactProjection, ItemProjection } from "../api/contracts.ts";
import { retouchPresentation } from "./retouchPresentation.ts";

const artifact = {
  artifact_id: "artifact-result",
  revision_id: "revision-result",
  family: "image",
  role: "deliverable",
  visibility: "primary",
  status: "ready",
  display_name: "result.png",
  mime_type: "image/png",
  size_bytes: 128,
  sha256: "a".repeat(64),
  created_at: "2026-07-10T00:00:00Z",
  lineage: { source_artifact_ids: [], supersedes_revision_id: null },
  renditions: [],
  actions: ["preview", "download", "feedback", "precise_retouch"],
  feedback: null,
  quality_evidence: { status: "passed", checks: [], score: 1, summary: "边缘检查通过。" },
} satisfies ArtifactProjection;

function item(content: Record<string, unknown>): ItemProjection {
  return {
    item_id: "item-result",
    thread_id: "thread-1",
    turn_id: "turn-retouch",
    kind: "artifact",
    status: "completed",
    content,
    inherited: false,
    created_at: "2026-07-10T00:00:00Z",
    updated_at: "2026-07-10T00:00:00Z",
  };
}

test("retouch presentation preserves backend change summary and inspection evidence", () => {
  const inspectionRegions = [
    {
      normalized_geometry: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 },
      summary: "标注区域已检查",
    },
    {
      normalized_geometry: { x: 0.5, y: 0.5 },
      summary: "边缘已检查",
    },
  ];
  assert.deepEqual(retouchPresentation(item({
    retouch_job_id: "retouch-1",
    artifact,
    change_summary: "移除了桌面水杯，并保持主体光线。",
    inspection_regions: inspectionRegions,
  })), {
    artifact,
    changeSummary: "移除了桌面水杯，并保持主体光线。",
    inspectionRegionCount: 2,
    inspectionRegions,
  });
});

test("ordinary artifact items never fabricate a retouch summary", () => {
  assert.equal(retouchPresentation(item({ artifact })), null);
});

test("quality evidence is the authoritative fallback when adapter summary is absent", () => {
  const result = retouchPresentation(item({
    retouch_job_id: "retouch-1",
    artifact,
    change_summary: null,
    inspection_regions: [],
  }));
  assert.equal(result?.changeSummary, "边缘检查通过。");
});

test("malformed inspection geometry is ignored instead of crashing result rendering", () => {
  const result = retouchPresentation(item({
    retouch_job_id: "retouch-1",
    artifact,
    inspection_regions: [
      { normalized_geometry: "not-an-object", summary: "bad" },
      { normalized_geometry: { x: 2, y: 0.2 }, summary: "outside" },
    ],
  }));
  assert.equal(result?.inspectionRegionCount, 0);
  assert.deepEqual(result?.inspectionRegions, []);
});
