import assert from "node:assert/strict";
import test from "node:test";

import type {
  ArtifactProjection,
  RetouchAnnotation,
  RetouchEditSurface,
  RetouchJobProjection,
  RetouchWorkspaceProjection,
} from "./contracts.ts";
import { validateArtifactBoundary } from "./artifactRuntimeContract.ts";
import { RuntimeContractError } from "./runtimeContract.ts";

const timestamp = "2026-07-15T06:30:00Z";

function artifact(revisionId = "rev_source"): ArtifactProjection {
  return {
    artifact_id: "art_target",
    revision_id: revisionId,
    family: "image",
    role: "deliverable",
    visibility: "primary",
    status: "ready",
    display_name: "海报_20260715-1430_01.png",
    mime_type: "image/png",
    size_bytes: 1024,
    sha256: "a".repeat(64),
    created_at: timestamp,
    lineage: {
      source_artifact_ids: [],
      supersedes_revision_id: revisionId === "rev_source" ? null : "rev_source",
    },
    renditions: [],
    actions: ["preview", "download", "feedback", "precise_retouch"],
    feedback: null,
    quality_evidence: {
      status: "passed",
      checks: [{ name: "raster", status: "passed", detail: null }],
      score: 0.99,
      summary: "检查通过",
    },
  };
}

function editSurface(
  revisionId = "rev_source",
  digest = "a".repeat(64),
): RetouchEditSurface {
  return {
    base_revision_id: revisionId,
    raster_digest: digest,
    width_px: 1200,
    height_px: 800,
    orientation: 1,
    color_space: "srgb",
    mime_type: "image/png",
    coordinate_space_version: "oriented-normalized-v1",
  };
}

function annotation(): RetouchAnnotation {
  return {
    kind: "rectangle",
    normalized_geometry: { x: 0.1, y: 0.2, width: 0.3, height: 0.2 },
    instruction: "只修改选中区域",
    annotation_id: "ann_one",
  };
}

function queuedJob(): RetouchJobProjection {
  return {
    job_id: "rtj_one",
    artifact_id: "art_target",
    base_revision_id: "rev_source",
    request: {
      base_revision_id: "rev_source",
      selected_artifact_ids: ["art_target"],
      agent_model_id: "ecorex-chat",
      image_model_id: "gpt-image-2",
      annotations: [annotation()],
      reference_artifact_ids: [],
      global_instruction: "保持未标注区域不变",
      client_request_id: "retouch_one",
      pinned_reference_revision_ids: {},
      edit_surface: editSurface(),
      mask: {
        schema_version: 1,
        coordinate_space_version: "oriented-normalized-v1",
        width_px: 1200,
        height_px: 800,
        sha256: "b".repeat(64),
        size_bytes: 256,
        covered_fraction: 0.06,
        pixel_regions: [{ x: 120, y: 160, width: 360, height: 160 }],
      },
    },
    status: "queued",
    created_at: timestamp,
    result_revision_id: null,
    change_summary: null,
    inspection_regions: [],
    failure_reason: null,
  };
}

function editingWorkspace(): RetouchWorkspaceProjection {
  return {
    workspace_id: "rtw_one",
    artifact_id: "art_target",
    version: 2,
    status: "editing",
    edit_surface: editSurface(),
    annotations: [annotation()],
    references: [],
    global_instruction: "保持未标注区域不变",
    view_state: {
      zoom: 1,
      pan_x: 0,
      pan_y: 0,
      selected_annotation_id: "ann_one",
      tool: "select",
    },
    mask: queuedJob().request.mask,
    submitted_job_id: null,
    job: null,
    result: null,
    result_surface: null,
    surface_url: "/api/v1/retouch-workspaces/rtw_one/surface",
    result_url: null,
    created_at: timestamp,
    updated_at: timestamp,
  };
}

function completedWorkspace(): RetouchWorkspaceProjection {
  const workspace = editingWorkspace();
  const job = queuedJob();
  job.status = "completed";
  job.result_revision_id = "rev_result";
  job.change_summary = "仅替换了标注区域";
  job.inspection_regions = [{
    normalized_geometry: { x: 0.1, y: 0.2, width: 0.3, height: 0.2 },
    summary: "目标区域已检查",
  }];
  return {
    ...workspace,
    version: 3,
    status: "submitted",
    submitted_job_id: job.job_id,
    job,
    result: artifact("rev_result"),
    result_surface: editSurface("rev_result"),
    result_url: "/api/v1/retouch-workspaces/rtw_one/result",
  };
}

function incompatible(operation: () => unknown): void {
  assert.throws(operation, RuntimeContractError);
}

test("Artifact boundary accepts the complete generated response family", () => {
  const item = artifact();
  assert.equal(
    (validateArtifactBoundary(item, "projection", { artifact_id: "art_target" }) as typeof item).artifact_id,
    "art_target",
  );
  assert.equal(
    (validateArtifactBoundary({ items: [item], count: 1 }, "list") as { count: number }).count,
    1,
  );
  assert.equal(
    (validateArtifactBoundary({
      feedback_id: "fb_one",
      revision_id: "rev_source",
      signal: "thumbs_up",
      recorded_at: timestamp,
    }, "feedback", { revision_id: "rev_source" }) as { signal: string }).signal,
    "thumbs_up",
  );
  assert.equal(
    (validateArtifactBoundary({
      artifact_id: "art_target",
      revision_id: "rev_source",
      action: "open",
      client_request_id: "open_one",
      status: "completed",
      requested_at: timestamp,
      updated_at: timestamp,
      failure_code: null,
    }, "action", {
      artifact_id: "art_target",
      revision_id: "rev_source",
      action: "open",
      client_request_id: "open_one",
    }) as { status: string }).status,
    "completed",
  );
  assert.equal(
    (validateArtifactBoundary(queuedJob(), "job", {
      artifact_id: "art_target",
      revision_id: "rev_source",
    }) as { status: string }).status,
    "queued",
  );
  assert.equal(
    (validateArtifactBoundary(completedWorkspace(), "workspace", {
      workspace_id: "rtw_one",
      artifact_id: "art_target",
      revision_id: "rev_source",
    }) as { status: string }).status,
    "submitted",
  );
});

test("Retouch brush geometry keeps the backend default width optional", () => {
  const workspace = editingWorkspace();
  workspace.annotations = [{
    annotation_id: "ann_brush",
    kind: "brush",
    normalized_geometry: {
      points: [{ x: 0.1, y: 0.2 }, { x: 0.3, y: 0.4 }],
    },
    instruction: "清理这条路径",
  }];
  workspace.view_state.selected_annotation_id = "ann_brush";
  assert.equal(
    (validateArtifactBoundary(workspace, "workspace") as RetouchWorkspaceProjection)
      .annotations[0].kind,
    "brush",
  );
});

test("Artifact boundary rejects internal, extra, duplicate and identity-drifted projections", () => {
  const internal = artifact() as unknown as Record<string, unknown>;
  internal.family = "source_code";
  incompatible(() => validateArtifactBoundary(internal, "projection"));

  const extra = { ...artifact(), storage_path: "C:/secret" };
  incompatible(() => validateArtifactBoundary(extra, "projection"));

  incompatible(() => validateArtifactBoundary({ items: [artifact()], count: 2 }, "list"));
  incompatible(() => validateArtifactBoundary(
    { items: [artifact(), artifact()], count: 2 },
    "list",
  ));
  incompatible(() => validateArtifactBoundary(artifact(), "projection", {
    artifact_id: "art_other",
  }));
});

test("Retouch boundary rejects Job, mask and workspace cross-identity drift", () => {
  const wrongTarget = queuedJob();
  wrongTarget.artifact_id = "art_other";
  incompatible(() => validateArtifactBoundary(wrongTarget, "job"));

  const earlyResult = queuedJob();
  earlyResult.result_revision_id = "rev_result";
  incompatible(() => validateArtifactBoundary(earlyResult, "job"));

  const badMask = queuedJob();
  badMask.request.mask!.pixel_regions[0].x = 1100;
  incompatible(() => validateArtifactBoundary(badMask, "job"));

  const wrongSurface = editingWorkspace();
  wrongSurface.surface_url = "/api/v1/retouch-workspaces/rtw_other/surface";
  incompatible(() => validateArtifactBoundary(wrongSurface, "workspace"));

  const wrongResult = completedWorkspace();
  wrongResult.result!.artifact_id = "art_other";
  incompatible(() => validateArtifactBoundary(wrongResult, "workspace"));

  const missingField = completedWorkspace() as unknown as Record<string, unknown>;
  delete missingField.result_url;
  incompatible(() => validateArtifactBoundary(missingField, "workspace"));
});
